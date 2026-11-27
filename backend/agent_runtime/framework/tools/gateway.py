"""Tool execution gateway: validation -> approval -> idempotent claim -> timeout -> OSS spill."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
import inspect
import json
import math
from typing import Any, List, Optional

from langgraph.types import interrupt

from agent_runtime.framework.core.contracts import (
    ExecutionContext,
    ReconcileResult,
    RiskLevel,
    ToolCall,
    ToolResult,
    canonical_hash,
    estimate_tokens,
)
from agent_runtime.framework.core.security import (
    AccessControlError,
    AuthorizationError,
    ResourceRef,
    enforce_check,
)
from agent_runtime.framework.tools.registry import IMMUTABLE_BUILTIN_NAMES, ToolRegistry, ToolSpec


def _error(call: ToolCall, code: str, detail: str = "") -> ToolResult:
    return ToolResult(call.id, call.name, f"[{code}] {detail}".rstrip(), ok=False, error_code=code)


def _json_value(value: Any) -> bool:
    if type(value) is dict:
        return all(isinstance(k, str) and _json_value(v) for k, v in value.items())
    if type(value) is list:
        return all(_json_value(v) for v in value)
    if type(value) is float:
        return math.isfinite(value)
    return value is None or type(value) in (int, str, bool)


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True


def _validate_schema_types(args: dict, schema: Optional[dict], required_args: tuple) -> Optional[str]:
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    for key, val in args.items():
        prop = props.get(key)
        if not isinstance(prop, dict):
            continue
        expected = prop.get("type")
        if not expected:
            continue
        if val is None and key not in required_args:
            continue
        allowed_types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_json_type(val, t) for t in allowed_types):
            return f"arg '{key}' expected {expected}, got {type(val).__name__}"
    return None


def _preview(text: str, budget: int) -> str:
    low, high = 0, min(len(text), 4 * budget + 3)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]


async def _invoke(spec: ToolSpec, args: dict, ctx: ExecutionContext) -> Any:
    if inspect.iscoroutinefunction(spec.handler):
        return await spec.handler(args, ctx)
    value = await asyncio.to_thread(spec.handler, args, ctx)
    return await value if inspect.isawaitable(value) else value


async def _invoke_reconcile(
    spec: ToolSpec, op_key: str, args: dict, ctx: ExecutionContext
) -> Any:
    fn = spec.reconcile_handler
    if fn is None:
        return None
    if inspect.iscoroutinefunction(fn):
        return await fn(op_key, args, ctx)
    value = await asyncio.to_thread(fn, op_key, args, ctx)
    return await value if inspect.isawaitable(value) else value


def compute_op_key(ctx: ExecutionContext, call: ToolCall) -> str:
    """Per-action idempotency key bound to persisted tool_call.id so multi-write turns never collide."""
    parts = [ctx.thread_id, ctx.auth_scope.user_id, ctx.request_id, call.name]
    if call.id:
        parts.append(call.id)
    return canonical_hash(parts)


class ToolGateway:
    def __init__(
        self,
        store,
        registry,
        tracer=None,
        max_result_tokens=450,
        loop_limit=3,
        middlewares=None,
        access_controller=None,
    ):
        if max_result_tokens < 1 or loop_limit < 1:
            raise ValueError("tool budgets must be positive")
        self.store = store
        self.registry = registry
        self.tracer = tracer
        self.max_result_tokens = max_result_tokens
        self.loop_limit = loop_limit
        self.middlewares = middlewares
        self.access_controller = access_controller

    async def execute_one(
        self, call: ToolCall, ctx: ExecutionContext, loop_counts=None
    ) -> ToolResult:
        if ctx.check_cancelled():
            raise asyncio.CancelledError("Run cancelled before tool execution")
        if self.middlewares is not None:
            return await self.middlewares.run_wrap_tool(
                call, ctx, lambda c, x: self._execute_core(c, x, loop_counts)
            )
        return await self._execute_core(call, ctx, loop_counts)

    async def _execute_core(
        self, call: ToolCall, ctx: ExecutionContext, loop_counts=None
    ) -> ToolResult:
        spec = self.registry.get(call.name)
        if spec is None:
            return _error(call, "unknown_tool")
        if not spec.permitted(ctx.auth_scope) or (ctx.depth and ctx.auth_scope.allowed_tools is None):
            return _error(call, "permission_denied")
        if self.access_controller is not None:
            try:
                await enforce_check(
                    self.access_controller,
                    ctx.auth_scope,
                    "tool:invoke",
                    ResourceRef(
                        kind="tool",
                        id=call.name,
                        attrs={"risk_level": spec.risk_level.value, "is_subagent": spec.is_subagent},
                    ),
                )
            except AuthorizationError as exc:
                return _error(call, "permission_denied", exc.reason)
            except AccessControlError as exc:
                return _error(call, "acl_error", str(exc))
        if spec.is_subagent and ctx.depth >= ctx.max_depth:
            return _error(call, "max_depth")
        if type(call.args) is not dict:
            return _error(call, "invalid_args", "expected an object")
        missing = set(spec.required_args).difference(call.args)
        extra = set(call.args).difference(spec.allowed_args)
        if missing or extra:
            return _error(call, "invalid_args", f"missing={sorted(missing)}, extra={sorted(map(str, extra))}")
        try:
            if not _json_value(call.args):
                return _error(call, "invalid_args", "expected JSON values")
            type_err = _validate_schema_types(call.args, spec.parameters_schema, spec.required_args)
            if type_err:
                return _error(call, "invalid_args", type_err)
            args = deepcopy(call.args)
            digest = canonical_hash(args)
        except (ValueError, RecursionError, UnicodeError):
            return _error(call, "invalid_args")

        if loop_counts is not None:
            signature = canonical_hash([call.name, digest])
            loop_counts[signature] = loop_counts.get(signature, 0) + 1
            if loop_counts[signature] > self.loop_limit:
                return _error(call, "loop_broken", "连续调用相同工具和参数，已停止。")

        op_key: Optional[str] = None
        if spec.risk_level != RiskLevel.READ:
            op_key = compute_op_key(ctx, call)
            review_id = None
            if spec.risk_level == RiskLevel.HIGH_RISK:
                ticket = self.store.get_or_create_approval(
                    ctx, op_key, call.name, digest, {"tool_name": call.name, "args": args}
                )
                if ticket.consumed_at is not None:
                    review_id = ticket.review_id
                else:
                    review_id = interrupt(asdict(ticket))
                    if review_id != ticket.review_id:
                        return _error(call, "approval_mismatch")
            acquired, run, error = self.store.authorize_and_claim_run(
                ctx, op_key, call.name, digest, review_id
            )
            if not acquired:
                if run.status == "reconcile" and spec.reconcile_handler is not None:
                    return await self._reconcile_existing(call, spec, args, ctx, op_key)
                if error or run.status != "done":
                    return _error(call, error or run.status)
                return ToolResult(
                    call.id,
                    call.name,
                    run.preview or "",
                    truncated=bool(run.result_ref),
                    ref_id=run.result_ref,
                    metadata={"cached": True, "op_key": op_key},
                )

        scope = (
            self.tracer.span(
                trace_id=ctx.trace_id,
                operation_name=f"tool:{call.name}",
                parent_span_id=ctx.parent_span_id,
            )
            if self.tracer
            else nullcontext()
        )
        with scope as span:
            child = replace(
                ctx,
                parent_span_id=span.span_id if span else ctx.parent_span_id,
                op_key=op_key,
            )
            try:
                value = await asyncio.wait_for(_invoke(spec, args, child), spec.timeout_sec)
                result = self._format_result(call, spec, ctx, value)
            except asyncio.CancelledError:
                if op_key:
                    self.store.finish_tool_run(
                        op_key, status="reconcile", error_code="cancelled"
                    )
                raise
            except asyncio.TimeoutError:
                result = _error(call, "timeout", "工具执行超时。")
            except Exception as exc:
                result = _error(call, "tool_error", type(exc).__name__)
            if span is not None:
                span.status = "ok" if result.ok else "error"
                span.attributes.update({"truncated": result.truncated, "error_code": result.error_code})
            if op_key:
                self.store.finish_tool_run(
                    op_key,
                    status="done" if result.ok else "reconcile",
                    receipt={"call_id": call.id} if result.ok else None,
                    preview=result.content,
                    result_ref=result.ref_id,
                    error_code=result.error_code,
                )
                result.metadata["op_key"] = op_key
            return result

    async def _reconcile_existing(
        self,
        call: ToolCall,
        spec: ToolSpec,
        args: dict,
        ctx: ExecutionContext,
        op_key: str,
    ) -> ToolResult:
        """Invoke a write tool's optional `reconcile(op_key, args, ctx)` callback to resolve uncertain runs."""
        scope = (
            self.tracer.span(
                trace_id=ctx.trace_id,
                operation_name=f"reconcile:{call.name}",
                parent_span_id=ctx.parent_span_id,
            )
            if self.tracer
            else nullcontext()
        )
        with scope as span:
            child = replace(
                ctx,
                parent_span_id=span.span_id if span else ctx.parent_span_id,
                op_key=op_key,
            )
            try:
                raw = await asyncio.wait_for(
                    _invoke_reconcile(spec, op_key, args, child),
                    spec.timeout_sec,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if span is not None:
                    span.status = "error"
                    span.attributes["error_code"] = type(exc).__name__
                return _error(call, "reconcile", f"对账查询失败: {type(exc).__name__}")

            if raw is None:
                outcome = ReconcileResult(status="pending")
            elif isinstance(raw, ReconcileResult):
                outcome = raw
            elif isinstance(raw, ToolResult):
                outcome = ReconcileResult(
                    status="done" if raw.ok else "failed",
                    content=raw.content,
                    error_code=raw.error_code,
                )
            else:
                text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                outcome = ReconcileResult(status="done", content=text)

            if outcome.status == "done":
                result = self._format_result(call, spec, ctx, outcome.content)
                self.store.finish_tool_run(
                    op_key,
                    status="done",
                    receipt=outcome.receipt or {"call_id": call.id, "reconciled": True},
                    preview=result.content,
                    result_ref=result.ref_id,
                    error_code=None,
                )
                result.metadata.update({"reconciled": True, "op_key": op_key})
                if span is not None:
                    span.status = "ok"
                    span.attributes["reconcile_status"] = "done"
                if self.tracer and hasattr(self.tracer, "log_event"):
                    self.tracer.log_event(
                        "tool_reconciled",
                        ctx.trace_id,
                        ctx.thread_id,
                        tool=call.name,
                        op_key=op_key,
                        outcome="done",
                    )
                return result

            if outcome.status == "failed":
                code = outcome.error_code or "reconcile_failed"
                result = _error(call, code, outcome.content)
                self.store.finish_tool_run(
                    op_key,
                    status="failed",
                    receipt=outcome.receipt,
                    preview=result.content,
                    error_code=code,
                )
                result.metadata.update({"reconciled": True, "op_key": op_key})
                if span is not None:
                    span.status = "error"
                    span.attributes["reconcile_status"] = "failed"
                return result

            if span is not None:
                span.status = "error"
                span.attributes["reconcile_status"] = "pending"
            return _error(call, "reconcile", outcome.content or "下游状态仍不确定，需继续对账。")

    async def reconcile_tool_run(
        self,
        op_key: str,
        *,
        tool_name: str,
        args: dict,
        ctx: ExecutionContext,
    ) -> ToolResult:
        """Public helper to explicitly trigger reconciliation for a `reconcile`-status `op_key`."""
        call = ToolCall(id=f"reconcile_{op_key[:12]}", name=tool_name, args=args)
        spec = self.registry.get(tool_name)
        if spec is None:
            return _error(call, "unknown_tool")
        if spec.reconcile_handler is None:
            return _error(call, "reconcile_unsupported", "该工具未注册 reconcile 回调")
        return await self._reconcile_existing(call, spec, deepcopy(args), ctx, op_key)

    def _format_result(self, call, spec, ctx, value) -> ToolResult:
        if isinstance(value, ToolResult):
            result = replace(value, tool_call_id=call.id, tool_name=call.name)
        else:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            result = ToolResult(call.id, call.name, text)
        if spec.bypass_truncation or estimate_tokens(result.content) <= self.max_result_tokens:
            return result
        placeholder = (
            "\n[truncated ref_id="
            + "0" * 36
            + ' chars=0..9999/999999 — 剩余 999999 字符，可调用 read_tool_output(ref_id="'
            + "0" * 36
            + '", offset=9999, limit=4000) 继续读取]'
        )
        preview = _preview(result.content, max(0, self.max_result_tokens - estimate_tokens(placeholder) - 1))
        blob = self.store.save_tool_output(ctx, call.name, result.content, preview)
        remaining = max(0, blob.total_chars - len(preview))
        hint = (
            f"\n[truncated ref_id={blob.ref_id} chars=0..{len(preview)}/{blob.total_chars}"
            f' — 剩余 {remaining} 字符，可调用 read_tool_output(ref_id="{blob.ref_id}", offset={len(preview)}, limit=4000) 继续读取]'
        )
        if estimate_tokens(preview + hint) <= self.max_result_tokens:
            preview += hint
        else:
            short_hint = f"\n[truncated ref_id={blob.ref_id} chars=0..{len(preview)}/{blob.total_chars}]"
            if estimate_tokens(preview + short_hint) <= self.max_result_tokens:
                preview += short_hint
        return replace(result, content=preview, truncated=True, ref_id=blob.ref_id)

    async def execute_batch(
        self,
        calls,
        ctx,
        loop_counts=None,
        max_parallel=4,
        *,
        completed_sink: Optional[List[ToolResult]] = None,
    ) -> List[ToolResult]:
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        counts = loop_counts if loop_counts is not None else {}
        results = completed_sink if completed_sink is not None else []
        pending = []

        async def drain_reads():
            if pending:
                batch = list(pending)
                pending.clear()
                outcomes = await asyncio.gather(
                    *(self.execute_one(c, ctx, counts) for c in batch),
                    return_exceptions=True,
                )
                first_exc: Optional[BaseException] = None
                for item in outcomes:
                    if isinstance(item, BaseException):
                        if first_exc is None:
                            first_exc = item
                    else:
                        results.append(item)
                if first_exc is not None:
                    raise first_exc

        for call in calls:
            spec = self.registry.get(call.name)
            if spec and spec.risk_level != RiskLevel.READ:
                await drain_reads()
                results.append(await self.execute_one(call, ctx, counts))
            else:
                pending.append(call)
                if len(pending) == max_parallel:
                    await drain_reads()
        await drain_reads()
        return results


__all__ = [
    "IMMUTABLE_BUILTIN_NAMES",
    "ToolGateway",
    "ToolRegistry",
    "ToolSpec",
    "compute_op_key",
]

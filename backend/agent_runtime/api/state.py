"""Thread Trace waterfall, summary cursor, workspace files, context budget, and event log snapshot route."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json

from fastapi import APIRouter, HTTPException, Request

from agent_runtime.api.deps import caller_scope, caller_scope_async, require_access
from agent_runtime.framework.context.memory import ContextBudget, _msg_tokens
from agent_runtime.framework.core.contracts import ExecutionContext, Message, estimate_tokens
from agent_runtime.framework.core.security import ResourceRef
from agent_runtime.infra.llm.provider import wire_spec

router = APIRouter(tags=["Conversation & Runs"])


def _compute_context_budget_snapshot(runtime, store, scope, thread_id: str, snapshot: dict) -> dict:
    budget: ContextBudget = getattr(runtime, "budget", None) or ContextBudget()
    system_prompt = getattr(runtime, "system_prompt", "") or ""
    registry = getattr(runtime, "registry", None)
    specs = registry.visible_specs(scope) if registry is not None else []
    wire_overhead = (
        estimate_tokens(json.dumps([wire_spec(s) for s in specs], ensure_ascii=False)) if specs else 0
    )
    fixed_tokens = _msg_tokens(Message("system", system_prompt.strip())) + wire_overhead
    history_budget = budget.history_budget(fixed_tokens)
    trigger_tokens = int(history_budget * budget.compact_trigger_ratio)

    summary_text = str((snapshot.get("summary") or {}).get("text") or "")
    cursor = int((snapshot.get("summary") or {}).get("covered_up_to_msg_id") or 0)
    summary_tokens = estimate_tokens(summary_text) if summary_text.strip() else 0
    active_messages = store.list_messages(thread_id, cursor) if thread_id else []
    active_tokens = sum(_msg_tokens(m) for m in active_messages)
    history_used_tokens = summary_tokens + active_tokens
    return {
        "max_window_tokens": budget.max_window_tokens,
        "max_input_tokens": budget.max_input_tokens,
        "fixed_tokens": fixed_tokens,
        "history_budget": history_budget,
        "compact_trigger_tokens": trigger_tokens,
        "compact_trigger_ratio": budget.compact_trigger_ratio,
        "summary_tokens": summary_tokens,
        "active_tokens": active_tokens,
        "history_used_tokens": history_used_tokens,
        "total_prompt_tokens_est": fixed_tokens + history_used_tokens,
    }


@router.get("/state", summary="获取会话 Trace 瀑布图、摘要游标、工作区文件与事件日志快照")
async def runtime_state(req: Request, thread_id: str = "", trace_id: str = ""):
    scope = await caller_scope_async(req)
    store, obs = req.app.state.store, req.app.state.obs
    try:
        snapshot = await asyncio.to_thread(
            store.get_thread_state_snapshot,
            scope,
            thread_id,
            trace_id,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc

    resolved_tid = snapshot.get("thread_id") or thread_id
    active_run = (
        await asyncio.to_thread(
            req.app.state.runtime.run_service.get_active_run_for_thread, scope, resolved_tid
        )
        if resolved_tid
        else None
    )
    logs = await obs.trace_logs(snapshot["trace_id"]) if (snapshot.get("trace_id") and hasattr(obs, "trace_logs")) else []
    context_budget = await asyncio.to_thread(
        _compute_context_budget_snapshot,
        req.app.state.runtime,
        store,
        scope,
        resolved_tid,
        snapshot,
    )
    return {
        **snapshot,
        "context_budget": context_budget,
        "active_run": asdict(active_run) if active_run else None,
        "loki_logs": logs,
    }


@router.get("/outputs/{ref_id}", summary="分页读取当前用户有权访问的 MinIO OSS 大工具输出工件")
def read_output_artifact(ref_id: str, req: Request, offset: int = 0, limit: int = 4000):
    scope = caller_scope(req)
    require_access(req, scope, "artifact:read", ResourceRef("artifact", id=ref_id, owner_id=scope.user_id))
    ctx = ExecutionContext(trace_id="artifact", thread_id="artifact", request_id="artifact", auth_scope=scope)
    page, err = req.app.state.store.read_tool_output_page(ctx, ref_id, offset=offset, limit=limit)
    if err:
        raise HTTPException(404, err)
    return {"ref_id": ref_id, "offset": offset, "content": page}

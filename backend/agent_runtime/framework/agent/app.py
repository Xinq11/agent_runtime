"""Unified `AgentRuntimeApp` SDK assembler — core kernel + pluggable extensions + frozen registry."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import importlib
import time
from typing import Any, Dict, List, Optional, Sequence

from agent_runtime.framework.context.memory import ContextBudget
from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.contracts import ApprovalDecision, AuthScope, ExecutionContext, Message
from agent_runtime.framework.core.langgraph_compat import default_checkpointer, make_command
from agent_runtime.framework.core.middleware import MiddlewareChain
from agent_runtime.framework.core.protocols import AgentMiddleware, AgentPlugin, AppMetadata
from agent_runtime.framework.core.runs import RunService, RunStateError
from agent_runtime.framework.core.workflow import (
    DEFAULT_SYSTEM_PROMPT,
    build_agent_graph,
    extract_interrupt_tickets,
    graph_config,
    invoke_phase,
    pending_tickets,
)
from agent_runtime.framework.tools.builtins import register_immutable_builtin_tools
from agent_runtime.framework.tools.events import EventGateway
from agent_runtime.framework.tools.gateway import ToolGateway
from agent_runtime.framework.tools.registry import ToolRegistry, ToolSpec
from agent_runtime.framework.tools.subagent import SubAgentProfile, register_subagent_tool


def load_extension(spec: str) -> AgentPlugin:
    """Dynamically load an extension or customer plugin by `module.path:Symbol`."""
    if ":" not in spec:
        raise ValueError(f"Invalid extension spec '{spec}'; expected 'module.path:Symbol'")
    mod_name, attr_name = spec.split(":", 1)
    mod = importlib.import_module(mod_name)
    obj = getattr(mod, attr_name)
    inst = obj() if callable(obj) else obj
    if hasattr(inst, "to_plugin") and not isinstance(inst, AgentPlugin):
        return inst.to_plugin()
    return inst


class AgentRuntimeApp:
    """Unified runtime container backing both Python SDK and FastAPI HTTP/SSE server."""

    def __init__(
        self,
        *,
        model: Optional[Any] = None,
        store: Optional[Any] = None,
        obs: Optional[Any] = None,
        checkpointer: Optional[Any] = None,
        extensions: Sequence[AgentPlugin] = (),
        tools: Sequence[ToolSpec] = (),
        subagents: Sequence[SubAgentProfile] = (),
        middlewares: Sequence[AgentMiddleware] = (),
        system_prompt: Optional[str] = None,
        budget: Optional[ContextBudget] = None,
        max_result_tokens: int = 220,
        loop_limit: int = 3,
        max_steps: int = 8,
        emit_tool_events: bool = True,
        on_event: Optional[Any] = None,
        prefer_real_langgraph: bool = True,
    ) -> None:
        if store is None:
            from agent_runtime.infra.memory.store import InMemoryStore

            store = InMemoryStore()
        if obs is None:
            from agent_runtime.infra.plg.hub import ObservabilityHub

            obs = ObservabilityHub(store)
        if model is None:
            from agent_runtime.infra.llm.mock import MockScriptedLLM

            model = MockScriptedLLM(obs=obs)

        self.store = store
        self.obs = obs
        self.model = model
        self.checkpointer = (
            checkpointer
            if checkpointer is not None
            else default_checkpointer(prefer_real_langgraph=prefer_real_langgraph)
        )
        self.run_service = RunService(self.store)
        self.budget = budget or ContextBudget()

        seen_names: set[str] = set()
        for ext in extensions:
            if ext.name in seen_names:
                raise ValueError(f"Duplicate extension name registered: '{ext.name}'")
            seen_names.add(ext.name)
        self.extensions: List[AgentPlugin] = list(extensions)

        self.middlewares = MiddlewareChain(middlewares)
        for ext in self.extensions:
            for mw in ext.middlewares():
                self.middlewares.add(mw)

        self.registry = ToolRegistry()
        register_immutable_builtin_tools(self.registry, self.store)

        gw_cls = EventGateway if emit_tool_events else ToolGateway
        self.gateway = gw_cls(
            store=self.store,
            registry=self.registry,
            tracer=self.obs,
            max_result_tokens=max_result_tokens,
            loop_limit=loop_limit,
            middlewares=self.middlewares,
        )

        for spec in tools:
            self.registry.register(spec)
        for profile in subagents:
            register_subagent_tool(
                registry=self.registry,
                profile=profile,
                model_client=self.model,
                tool_gateway=self.gateway,
                obs=self.obs,
                tool_spec_cls=ToolSpec,
            )
        for ext in self.extensions:
            ext.register_tools(
                registry=self.registry,
                store=self.store,
                model=self.model,
                gateway=self.gateway,
                obs=self.obs,
            )
        self.registry.freeze()

        metas = [m for ext in self.extensions if (m := ext.metadata()) is not None]
        if len(metas) > 1:
            names = [m.plugin_name for m in metas]
            raise ValueError(
                f"Multiple extensions define AppMetadata: {names}; only one primary customer extension may set UI branding"
            )
        self._metadata = metas[0] if metas else AppMetadata()

        identities = [i for ext in self.extensions if (i := ext.default_identity()) is not None]
        self._default_identity = (
            identities[-1]
            if identities
            else AuthScope(tenant_id="default", user_id="local_user", roles=("user",))
        )

        if system_prompt is not None:
            self.system_prompt = system_prompt
        else:
            parts = [ext.system_prompt.strip() for ext in self.extensions if ext.system_prompt.strip()]
            self.system_prompt = "\n\n".join(parts) if parts else DEFAULT_SYSTEM_PROMPT

        self.graph = build_agent_graph(
            store=self.store,
            obs=self.obs,
            model=self.model,
            gateway=self.gateway,
            checkpointer=self.checkpointer,
            system_prompt=self.system_prompt,
            middlewares=self.middlewares,
            budget=self.budget,
            max_steps=max_steps,
            on_event=on_event,
            cancel_checker=self.run_service.is_cancelled,
            prefer_real_langgraph=prefer_real_langgraph,
        )

    def bootstrap_extensions(self) -> None:
        """Invoke idempotent extension bootstrap hooks (seed knowledge, default skills, etc.)."""
        for ext in self.extensions:
            ext.bootstrap_store(self.store)

    def metadata(self) -> AppMetadata:
        return self._metadata

    def default_identity(self) -> AuthScope:
        return self._default_identity

    async def execute_run_state(
        self,
        *,
        run_id: str,
        scope: AuthScope,
        thread_id: str,
        trace_id: str,
        request_id: str,
        start_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Single graph execution path shared by both Python SDK and FastAPI SSE."""
        t0 = start_ms if start_ms is not None else int(time.time() * 1000)
        state = {
            "run_id": run_id,
            "thread_id": thread_id,
            "trace_id": trace_id,
            "request_id": request_id,
            "scope": asdict(scope),
            "messages": [],
            "persisted_count": 0,
            "step": 0,
            "counts": {},
            "start_ms": t0,
        }
        result = await invoke_phase(self.graph, self.store, state, graph_config(thread_id), trace_id, t0)
        if result.get("__interrupt__"):
            result["__interrupt__"] = extract_interrupt_tickets(result["__interrupt__"])
            self.run_service.mark_paused(run_id, scope.tenant_id)
        else:
            self.run_service.mark_finished(run_id, scope.tenant_id, status="completed")
        result["run_id"] = run_id
        return result

    async def run_turn(
        self,
        user_text: str,
        *,
        ctx: Optional[ExecutionContext] = None,
        thread_id: Optional[str] = None,
        scope: Optional[AuthScope] = None,
    ) -> Dict[str, Any]:
        """Execute a single turn via the unified LangGraph engine + durable RunService."""
        if ctx is None:
            tid = thread_id or uid("th")
            ctx = ExecutionContext(
                trace_id=uid("tr"),
                thread_id=tid,
                request_id=uid("req"),
                auth_scope=scope or self.default_identity(),
            )
        run = self.run_service.start_run(
            scope=ctx.auth_scope,
            thread_id=ctx.thread_id,
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            run_id=ctx.run_id or None,
        )
        ctx.run_id = run.run_id
        self.store.append_message(ctx.thread_id, Message(role="user", content=user_text))

        try:
            return await self.execute_run_state(
                run_id=run.run_id,
                scope=ctx.auth_scope,
                thread_id=ctx.thread_id,
                trace_id=ctx.trace_id,
                request_id=ctx.request_id,
            )
        except asyncio.CancelledError:
            self.run_service.mark_finished(run.run_id, ctx.auth_scope.tenant_id, status="cancelled")
            return {"run_id": run.run_id, "status": "cancelled", "final_answer": ""}
        except Exception as exc:
            self.run_service.mark_finished(
                run.run_id, ctx.auth_scope.tenant_id, status="failed", error_detail=type(exc).__name__
            )
            raise

    async def abort_run(
        self,
        *,
        scope: Optional[AuthScope] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ):
        return self.run_service.request_abort(
            scope=scope or self.default_identity(),
            run_id=run_id,
            thread_id=thread_id,
        )

    async def resume_approval(
        self,
        *,
        thread_id: str,
        decision: ApprovalDecision,
        scope: Optional[AuthScope] = None,
    ) -> Dict[str, Any]:
        """Single unified approval-resume path for both Python SDK and HTTP API.

        Validates checkpoint ticket & records decision cleanly so invalid/expired
        tickets never leave the run stranded in `running`.
        """
        caller = scope or self.default_identity()
        cfg = graph_config(thread_id)
        snapshot = await self.graph.aget_state(cfg)
        if decision.review_id not in {v["review_id"] for v in pending_tickets(snapshot)}:
            raise RunStateError("审批单未处于暂停状态")

        vals = getattr(snapshot, "values", {}) or {}
        run_id = vals.get("run_id")
        if run_id:
            rec = self.run_service.get_run(caller, run_id)
            if rec.status != "paused":
                raise RunStateError("该运行已取消或不再处于暂停状态，无法恢复")

        # Record decision BEFORE transitioning run to `running`; then atomically claim resume
        self.store.record_approval_decision(decision)
        if run_id:
            self.run_service.mark_resumed(run_id, caller.tenant_id)

        trace_id = vals.get("trace_id") or uid("tr")
        start_ms = int(vals.get("start_ms") or time.time() * 1000)
        try:
            result = await invoke_phase(
                self.graph,
                self.store,
                make_command(resume=decision.review_id),
                cfg,
                trace_id,
                start_ms,
            )
            tickets = extract_interrupt_tickets(result.get("__interrupt__", []))
            if tickets:
                result["__interrupt__"] = tickets
                if run_id:
                    self.run_service.mark_paused(run_id, caller.tenant_id)
            else:
                if run_id:
                    self.run_service.mark_finished(run_id, caller.tenant_id, status="completed")
            if run_id:
                result["run_id"] = run_id
            return result
        except asyncio.CancelledError:
            if run_id:
                self.run_service.mark_finished(run_id, caller.tenant_id, status="cancelled")
            return {"run_id": run_id, "status": "cancelled", "final_answer": ""}
        except Exception as exc:
            if run_id:
                self.run_service.mark_finished(
                    run_id, caller.tenant_id, status="failed", error_detail=type(exc).__name__
                )
            raise

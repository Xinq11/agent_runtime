"""Agent as Tool: lightweight read-only Sub-Agent runner sharing core Harness primitives.

Kept intentionally outside LangGraph's checkpointer so ephemeral read-only sub-agents
never bloat checkpoint storage while reusing the exact same `assemble_before_model`,
`filter_visible_tools`, `invoke_model_step`, `strip_dsml_markup`, and `ToolGateway`
invariants as the main workflow.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
from typing import Any, Dict, List, Optional, Tuple

from agent_runtime.framework.context.memory import ContextBudget, assemble_before_model
from agent_runtime.framework.core.contracts import ExecutionContext, Message, RiskLevel, estimate_tokens
from agent_runtime.framework.core.model_step import filter_visible_tools, invoke_model_step
from agent_runtime.infra.llm.provider import strip_dsml_markup, wire_spec

_SUBAGENT_FINAL_NOTICE = "\n\n[运行时提示：子任务已达步数上限，请禁止再调用工具，直接汇总已有结论。]"


@dataclass(frozen=True)
class SubAgentProfile:
    """The parent calls `call_<subagent_name>`; events use the bare name as the role."""

    subagent_name: str
    description: str
    system_prompt: str
    allowed_tools: Tuple[str, ...]
    model_name: str = "deepseek-chat"
    max_steps: int = 4
    timeout_sec: float = 12.0
    budget: Optional[ContextBudget] = None

    def __post_init__(self) -> None:
        if not isinstance(self.subagent_name, str) or not self.subagent_name.strip():
            raise ValueError("subagent_name must be a non-empty string")


async def run_readonly_subagent(
    *,
    profile: SubAgentProfile,
    goal: str,
    parent_ctx: ExecutionContext,
    model_client: Any,
    tool_gateway: Any,
    obs: Any,
    budget: Optional[ContextBudget] = None,
) -> str:
    """Execute a read-only sub-agent in an isolated, budget-bounded context."""
    if parent_ctx.depth >= parent_ctx.max_depth:
        return "[ERROR] 已达最大子 Agent 嵌套深度 (max_depth=1)，禁止继续派发子 Agent。"

    active_budget = profile.budget or budget or ContextBudget()
    sys_prompt = (
        f"{profile.system_prompt}\n"
        "约束：你处于只读调研模式，只输出最终结论、关键数字和出处，勿输出冗长试错过程。"
    )

    with obs.span(
        trace_id=parent_ctx.trace_id,
        operation_name=f"subagent:{profile.subagent_name}",
        parent_span_id=parent_ctx.parent_span_id,
        attributes={"role": profile.subagent_name, "model": profile.model_name},
    ) as sub_span:
        sub_ctx = parent_ctx.derive_subagent(
            agent_name=profile.subagent_name,
            allowed_tools=profile.allowed_tools,
            parent_span_id=sub_span.span_id,
            model_name=profile.model_name,
        )
        await parent_ctx.emit(
            "subagent_status",
            {"role": profile.subagent_name, "phase": "started", "goal": goal},
        )

        # Isolated private conversation window — budget-trimmed each step and discarded on return
        private_msgs: List[Message] = [Message(role="user", content=goal)]
        loop_counts: Dict[str, int] = {}
        final_answer = ""
        steps_used = 0

        for step_idx in range(profile.max_steps):
            if sub_ctx.check_cancelled():
                raise asyncio.CancelledError("Sub-agent cancelled by user")
            steps_used = step_idx + 1
            visible_specs = await filter_visible_tools(
                tool_gateway.registry,
                sub_ctx,
                access_controller=getattr(tool_gateway, "access_controller", None),
                include_subagents=False,
            )
            is_final_step = step_idx >= profile.max_steps - 1
            wire_overhead = (
                estimate_tokens(json.dumps([wire_spec(s) for s in visible_specs], ensure_ascii=False))
                if visible_specs
                else 0
            )
            assembled, _ = assemble_before_model(
                system_prompt=sys_prompt,
                summary_text="",
                recent_messages=private_msgs,
                budget=active_budget,
                wire_overhead=wire_overhead,
            )
            resp = await invoke_model_step(
                model=model_client,
                messages=assembled,
                tool_specs=visible_specs,
                ctx=sub_ctx,
                is_final_step=is_final_step,
                convergence_notice=_SUBAGENT_FINAL_NOTICE,
            )
            cleaned = strip_dsml_markup(resp.content or "")
            if cleaned != (resp.content or ""):
                resp = replace(resp, content=cleaned)
            private_msgs.append(resp)

            if not resp.tool_calls or is_final_step:
                final_answer = (resp.content or "").strip() or (
                    "[SUBAGENT_MAX_STEPS] 子任务已达步数上限，请基于已有部分结论汇报。"
                )
                break

            results = await tool_gateway.execute_batch(
                resp.tool_calls,
                sub_ctx,
                loop_counts=loop_counts,
            )
            for res in results:
                private_msgs.append(
                    Message(
                        role="tool",
                        content=res.content,
                        tool_call_id=res.tool_call_id,
                        name=res.tool_name,
                    )
                )
        else:
            final_answer = "[SUBAGENT_MAX_STEPS] 子任务已达步数上限，请基于已有部分结论汇报。"

        sub_span.attributes.update({"steps": steps_used, "reply_chars": len(final_answer)})
        if hasattr(obs, "log_event"):
            obs.log_event(
                "subagent_done",
                parent_ctx.trace_id,
                parent_ctx.thread_id,
                role=profile.subagent_name,
                steps=steps_used,
                chars=len(final_answer),
            )
        await parent_ctx.emit(
            "subagent_status",
            {"role": profile.subagent_name, "phase": "done", "chars": len(final_answer)},
        )
        return final_answer


def register_subagent_tool(
    *,
    registry: Any,
    profile: SubAgentProfile,
    model_client: Any,
    tool_gateway: Any,
    obs: Any,
    tool_spec_cls: Any,
    budget: Optional[ContextBudget] = None,
) -> None:
    """Register a read-only Sub-Agent as a normal Tool on the shared ToolRegistry."""

    async def _handler(args: Dict[str, Any], ctx: ExecutionContext) -> str:
        return await run_readonly_subagent(
            profile=profile,
            goal=str(args["goal"]),
            parent_ctx=ctx,
            model_client=model_client,
            tool_gateway=tool_gateway,
            obs=obs,
            budget=budget,
        )

    registry.register(
        tool_spec_cls(
            name=f"call_{profile.subagent_name}",
            description=profile.description,
            required_args=("goal",),
            allowed_args=("goal",),
            risk_level=RiskLevel.READ,
            handler=_handler,
            timeout_sec=profile.timeout_sec,
            is_subagent=True,
        )
    )

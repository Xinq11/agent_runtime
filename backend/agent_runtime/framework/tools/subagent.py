"""Agent as Tool: read-only Sub-Agent runner reusing the unified ToolGateway.

Follows the refactored `agent-multi-agent-collaboration.html` design:
  - Sub-agents run inside an isolated private `messages[]` window.
  - `max_depth = 1` prevents recursive sub-agent spawning.
  - Permissions are monotonically narrowed (`allow_write=False`, role allowlist).
  - Return value flows directly through `ToolGateway`'s truncation & `ref_id` spill.
  - Progress events stream out-of-band via `ctx.emit("subagent_status", ...)`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import inspect
from typing import Any, Dict, List, Tuple

from agent_runtime.framework.core.contracts import ExecutionContext, Message, RiskLevel


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
) -> str:
    """Execute a read-only sub-agent in an isolated context and return concise text."""
    if parent_ctx.depth >= parent_ctx.max_depth:
        return "[ERROR] 已达最大子 Agent 嵌套深度 (max_depth=1)，禁止继续派发子 Agent。"

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

        # Isolated private conversation window — discarded on return
        private_msgs: List[Message] = [
            Message(
                role="system",
                content=(
                    f"{profile.system_prompt}\n"
                    "约束：你处于只读调研模式，只输出最终结论、关键数字和出处，勿输出冗长试错过程。"
                ),
            ),
            Message(role="user", content=goal),
        ]
        loop_counts: Dict[str, int] = {}
        final_answer = ""

        for step_idx in range(profile.max_steps):
            raw_specs = tool_gateway.registry.visible_specs(
                sub_ctx.auth_scope, include_subagents=False
            )
            ac = getattr(tool_gateway, "access_controller", None)
            if ac is not None and raw_specs:
                from agent_runtime.framework.core.security import (
                    AuthorizationError,
                    ResourceRef,
                    enforce_check,
                )

                visible_specs = []
                for sp in raw_specs:
                    try:
                        await enforce_check(
                            ac,
                            sub_ctx.auth_scope,
                            "tool:list",
                            ResourceRef(
                                kind="tool",
                                id=sp.name,
                                tenant_id=sub_ctx.auth_scope.tenant_id,
                                attrs={"risk_level": sp.risk_level.value, "is_subagent": False},
                            ),
                        )
                        visible_specs.append(sp)
                    except AuthorizationError:
                        continue
            else:
                visible_specs = raw_specs
            is_final_step = step_idx >= profile.max_steps - 1
            call_msgs = list(private_msgs)
            if is_final_step:
                notice = "\n\n[运行时提示：子任务已达步数上限，请禁止再调用工具，直接汇总已有结论。]"
                if call_msgs and call_msgs[-1].role == "tool":
                    call_msgs[-1] = replace(call_msgs[-1], content=(call_msgs[-1].content or "") + notice)
                else:
                    call_msgs.append(Message(role="user", content=notice.strip()))
                gen_sig = inspect.signature(model_client.generate)
                supports_tc = "tool_choice" in gen_sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in gen_sig.parameters.values()
                )
                if supports_tc:
                    resp = await model_client.generate(
                        messages=call_msgs,
                        tool_specs=visible_specs,
                        ctx=sub_ctx,
                        tool_choice="none",
                    )
                else:
                    resp = await model_client.generate(messages=call_msgs, tool_specs=[], ctx=sub_ctx)
            else:
                resp = await model_client.generate(
                    messages=call_msgs,
                    tool_specs=visible_specs,
                    ctx=sub_ctx,
                )
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

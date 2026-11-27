"""Shared single-step model invocation & tool-visibility filtering helper.

Reused by both the main LangGraph loop (`workflow.py`) and read-only sub-agents
(`subagent.py`) to keep tool:list enforcement, final-step `tool_choice="none"`
convergence, and DSML cleanup consistent in one place.
"""

from __future__ import annotations

from dataclasses import replace
import inspect
from typing import Any, List, Optional, Sequence

from agent_runtime.framework.core.contracts import ExecutionContext, Message
from agent_runtime.framework.core.security import AuthorizationError, ResourceRef, enforce_check
from agent_runtime.infra.llm.provider import strip_dsml_markup


def supports_tool_choice(model: Any) -> bool:
    gen = getattr(model, "generate", None)
    if gen is None:
        return False
    try:
        sig = inspect.signature(gen)
    except (TypeError, ValueError):
        return False
    if "tool_choice" in sig.parameters:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def append_convergence_notice(messages: Sequence[Message], notice: str) -> List[Message]:
    """Attach a transient final-step notice without inserting mid-conversation system/user turns."""
    out = list(messages)
    if out and out[-1].role == "tool":
        out[-1] = replace(out[-1], content=(out[-1].content or "") + notice)
    else:
        out.append(Message(role="user", content=notice.strip()))
    return out


async def filter_visible_tools(
    registry: Any,
    ctx: ExecutionContext,
    *,
    access_controller: Optional[Any] = None,
    include_subagents: bool = True,
) -> List[Any]:
    """Return tools permitted by scope + AccessController `tool:list` checks."""
    raw_specs = registry.visible_specs(ctx.auth_scope, include_subagents=include_subagents)
    if access_controller is None or not raw_specs:
        return list(raw_specs)
    specs = []
    for sp in raw_specs:
        try:
            await enforce_check(
                access_controller,
                ctx.auth_scope,
                "tool:list",
                ResourceRef(
                    kind="tool",
                    id=sp.name,
                    attrs={"risk_level": sp.risk_level.value, "is_subagent": sp.is_subagent},
                ),
            )
            specs.append(sp)
        except AuthorizationError:
            continue
    return specs


async def invoke_model_step(
    *,
    model: Any,
    messages: Sequence[Message],
    tool_specs: Sequence[Any],
    ctx: ExecutionContext,
    is_final_step: bool,
    convergence_notice: str,
) -> Message:
    """Invoke `model.generate` once, preserving tools + `tool_choice='none'` on final steps and stripping DSML."""
    if is_final_step:
        call_inputs = append_convergence_notice(messages, convergence_notice)
        if supports_tool_choice(model):
            msg = await model.generate(
                messages=call_inputs,
                tool_specs=tool_specs,
                ctx=ctx,
                tool_choice="none",
            )
        else:
            msg = await model.generate(messages=call_inputs, tool_specs=[], ctx=ctx)
    else:
        msg = await model.generate(messages=messages, tool_specs=tool_specs, ctx=ctx)

    cleaned = strip_dsml_markup(msg.content or "")
    if cleaned != (msg.content or ""):
        msg = replace(msg, content=cleaned)
    return msg

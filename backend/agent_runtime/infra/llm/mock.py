"""Generic `ModelClient` base and deterministic `MockScriptedLLM` for offline tests/demos."""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence, Set

from agent_runtime.framework.core.contracts import ExecutionContext, Message, estimate_tokens

ScriptRouter = Callable[[ExecutionContext, str, List[Message], Set[str]], Message]


class ModelClient:
    """Base protocol for LLM backends."""

    async def generate(
        self,
        *,
        messages: Sequence[Message],
        tool_specs: Sequence[Any],
        ctx: ExecutionContext,
    ) -> Message:
        raise NotImplementedError


def _default_router(
    ctx: ExecutionContext,
    last_user: str,
    tool_msgs: List[Message],
    tool_names: Set[str],
) -> Message:
    return Message(role="assistant", content=f"已收到消息：{last_user}")


class MockScriptedLLM(ModelClient):
    """Deterministic zero-dependency LLM for unit tests and local offline demos."""

    def __init__(
        self,
        obs: Optional[Any] = None,
        *,
        router: Optional[ScriptRouter] = None,
    ) -> None:
        self.obs = obs
        self.router = router or _default_router
        self.calls: List[dict] = []

    def set_default_mock_router(self, router: ScriptRouter) -> None:
        if self.router is _default_router:
            self.router = router

    async def generate(
        self,
        *,
        messages: Sequence[Message],
        tool_specs: Sequence[Any],
        ctx: ExecutionContext,
    ) -> Message:
        in_tok = sum(estimate_tokens(m.content) for m in messages)
        cache_tok = int(in_tok * 0.72) if len(messages) > 2 else 0
        self.calls.append({"agent": ctx.agent_name, "depth": ctx.depth, "in_tokens": in_tok})

        tool_names = {getattr(t, "name", "") for t in tool_specs}
        user_msgs = [m.content for m in messages if m.role == "user"]
        last_user = user_msgs[-1] if user_msgs else ""
        tool_msgs = [m for m in messages if m.role == "tool"]

        reply = self.router(ctx, last_user, tool_msgs, tool_names)

        out_tok = estimate_tokens(reply.content) + len(reply.tool_calls) * 24
        if self.obs is not None:
            self.obs.metrics.inc(
                "agent_tokens_total", in_tok, agent=ctx.agent_name, model=ctx.model_name, kind="in"
            )
            self.obs.metrics.inc(
                "agent_tokens_total",
                cache_tok,
                agent=ctx.agent_name,
                model=ctx.model_name,
                kind="cache_read",
            )
            self.obs.metrics.inc(
                "agent_tokens_total", out_tok, agent=ctx.agent_name, model=ctx.model_name, kind="out"
            )
        return reply

"""Composable async middleware pipeline for `agent_runtime`."""

from __future__ import annotations

from typing import Awaitable, Callable, List, Sequence

from agent_runtime.framework.core.contracts import ExecutionContext, Message, ToolCall, ToolResult
from agent_runtime.framework.core.protocols import AgentMiddleware


class MiddlewareChain:
    """Executes registered `AgentMiddleware` hooks in deterministic registration order."""

    def __init__(self, middlewares: Sequence[AgentMiddleware] = ()) -> None:
        self._items: List[AgentMiddleware] = list(middlewares)

    def add(self, mw: AgentMiddleware) -> "MiddlewareChain":
        self._items.append(mw)
        return self

    @property
    def items(self) -> List[AgentMiddleware]:
        return list(self._items)

    async def run_before_model(
        self, messages: List[Message], ctx: ExecutionContext
    ) -> List[Message]:
        current = messages
        for mw in self._items:
            current = await mw.before_model(current, ctx)
        return current

    async def run_after_model(
        self, response: Message, ctx: ExecutionContext
    ) -> Message:
        current = response
        for mw in reversed(self._items):
            current = await mw.after_model(current, ctx)
        return current

    async def run_wrap_tool(
        self,
        call: ToolCall,
        ctx: ExecutionContext,
        terminal: Callable[[ToolCall, ExecutionContext], Awaitable[ToolResult]],
    ) -> ToolResult:
        handler = terminal
        for mw in reversed(self._items):
            prev = handler

            async def _wrapped(
                c: ToolCall,
                x: ExecutionContext,
                _mw: AgentMiddleware = mw,
                _next: Callable[[ToolCall, ExecutionContext], Awaitable[ToolResult]] = prev,
            ) -> ToolResult:
                return await _mw.wrap_tool(c, x, _next)

            handler = _wrapped
        return await handler(call, ctx)

    async def run_after_turn(self, final_answer: str, ctx: ExecutionContext) -> None:
        for mw in self._items:
            await mw.after_turn(final_answer, ctx)

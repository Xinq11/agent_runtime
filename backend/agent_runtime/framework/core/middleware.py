"""Composable async middleware pipeline for `agent_runtime`.

Enforces context-window safety: middlewares can contribute budgeted extra context,
transform user text, observe immutable assembled snapshots, and wrap tools without
being able to corrupt system prompts or atomic tool-call message pairs.
"""

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

    async def run_transform_user_input(self, text: str, ctx: ExecutionContext) -> str:
        current = text
        for mw in self._items:
            updated = await mw.transform_user_input(current, ctx)
            if isinstance(updated, str):
                current = updated
        return current

    async def run_extra_context(self, latest_user_text: str, ctx: ExecutionContext) -> List[str]:
        blocks: List[str] = []
        for mw in self._items:
            chunk = await mw.extra_context(latest_user_text, ctx)
            if isinstance(chunk, str) and chunk.strip():
                blocks.append(chunk.strip())
        return blocks

    async def run_on_before_model(
        self, messages: Sequence[Message], ctx: ExecutionContext
    ) -> None:
        snapshot = tuple(messages)
        for mw in self._items:
            await mw.on_before_model(snapshot, ctx)

    async def run_after_model(
        self, response: Message, ctx: ExecutionContext
    ) -> Message:
        current = response
        for mw in reversed(self._items):
            updated = await mw.after_model(current, ctx)
            if isinstance(updated, Message):
                current = updated
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

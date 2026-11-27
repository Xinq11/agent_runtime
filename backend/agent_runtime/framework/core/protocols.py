"""Public core extension protocols for the `agent_runtime` SDK.

Downstream teams implement these stable protocols (or subclass `AgentPlugin` /
`AgentMiddleware`) without modifying framework internals.
Zero RAG, Skills, or FastAPI hard dependencies live in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, Sequence, Tuple, runtime_checkable

from agent_runtime.framework.core.contracts import (
    AuthScope,
    ExecutionContext,
    Message,
    ToolCall,
    ToolResult,
)


@dataclass(frozen=True)
class QuickPromptSpec:
    """Suggested prompt card dynamically exposed via `/api/v1/meta`."""

    title: str
    hint: str
    prompt: str


@dataclass(frozen=True)
class AppMetadata:
    """Metadata exposed via `/api/v1/meta` so any pluggable frontend adapts dynamically."""

    app_name: str = "Agent Runtime"
    subtitle: str = "Workspace"
    plugin_name: str = "core"
    welcome_title: str = "今天想处理哪项任务？"
    welcome_description: str = (
        "通用 Agent Runtime 底座：支持流式对话与随时中止、云原生文件读写、上下文预算压缩、子 Agent 委派、高危审批恢复与全链路 Trace 观测。"
    )
    quick_prompts: Tuple[QuickPromptSpec, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_name": self.app_name,
            "subtitle": self.subtitle,
            "plugin_name": self.plugin_name,
            "welcome_title": self.welcome_title,
            "welcome_description": self.welcome_description,
            "quick_prompts": [
                {"title": q.title, "hint": q.hint, "prompt": q.prompt}
                for q in self.quick_prompts
            ],
        }


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for any pluggable chat-completion / tool-calling model backend."""

    async def generate(
        self,
        *,
        messages: Sequence[Message],
        tool_specs: Sequence[Any],
        ctx: ExecutionContext,
    ) -> Message: ...


class AgentMiddleware:
    """Base class for safe, scoped lifecycle hooks around model & tool execution.

    Context-window ownership stays strictly inside the runtime kernel (`assemble_before_model`):
    middlewares cannot overwrite `system_prompt`, drop history summaries, or break atomic
    `assistant(tool_calls)` / `tool` message pairs.
    """

    name: str = "middleware"

    async def extra_context(self, latest_user_text: str, ctx: ExecutionContext) -> Optional[str]:
        """Optionally return a reference text block merged under kernel ContextBudget."""
        return None

    async def transform_user_input(self, text: str, ctx: ExecutionContext) -> str:
        """Optionally transform/redact the latest user text before model inference."""
        return text

    async def on_before_model(
        self, messages: Tuple[Message, ...], ctx: ExecutionContext
    ) -> None:
        """Read-only observation hook invoked with an immutable snapshot before model call."""
        return None

    async def after_model(
        self, response: Message, ctx: ExecutionContext
    ) -> Message:
        return response

    async def wrap_tool(
        self,
        call: ToolCall,
        ctx: ExecutionContext,
        next_call: Callable[[ToolCall, ExecutionContext], Awaitable[ToolResult]],
    ) -> ToolResult:
        return await next_call(call, ctx)

    async def after_turn(self, final_answer: str, ctx: ExecutionContext) -> None:
        return None


class AgentPlugin:
    """Base class for modular extensions & customer delivery packs.

    Plugins contribute tools, declarative read-only sub-agents (`subagents()`),
    middlewares, prompt segments, optional HTTP routers (`routers()`),
    storage bootstrapping, and UI metadata without touching core internals.
    """

    name: str = "custom"
    system_prompt: str = ""

    def metadata(self) -> Optional[AppMetadata]:
        return None

    def register_tools(
        self,
        *,
        registry: Any,
        store: Any,
        model: Any,
        gateway: Any,
        obs: Any,
    ) -> None:
        return None

    def subagents(self) -> Sequence[Any]:
        """Return `SubAgentProfile` declarations auto-wired by `AgentRuntimeApp`."""
        return ()

    def middlewares(self) -> Sequence[AgentMiddleware]:
        return ()

    def routers(self) -> Sequence[Any]:
        """Optionally return FastAPI `APIRouter` instances mounted when running the HTTP server."""
        return ()

    def bootstrap_store(self, store: Any) -> None:
        return None

    def default_identity(self) -> Optional[AuthScope]:
        return None

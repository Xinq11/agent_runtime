"""Public extension protocols for the `agent_runtime` SDK.

Downstream teams implement these stable protocols (or subclass `BaseAgent` /
`AgentPlugin` / `AgentMiddleware`) without modifying framework internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from agent_runtime.framework.core.contracts import (
    AuthScope,
    ExecutionContext,
    Message,
    RetrievalResult,
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
        "通用 Agent Runtime 底座：支持流式对话与随时中止、云原生文件读写、上下文预算压缩、高危审批恢复、RAG 知识检索、Skills 与全链路 Trace 观测。"
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


@runtime_checkable
class RAGStrategy(Protocol):
    """Protocol for pluggable custom RAG strategies (CRAG, Milvus, ES, Dify, etc.)."""

    async def retrieve(
        self,
        *,
        scope: AuthScope,
        query: str,
        top_k: int = 4,
        ctx: Optional[ExecutionContext] = None,
    ) -> RetrievalResult: ...


class AgentMiddleware:
    """Base class for plug-and-play lifecycle hooks around model & tool execution."""

    name: str = "middleware"

    async def before_model(
        self, messages: List[Message], ctx: ExecutionContext
    ) -> List[Message]:
        return messages

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

    Plugins can contribute tools, read-only sub-agents, middlewares, prompt segments,
    seed documents/skills, and Studio metadata without touching core internals.
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

    def middlewares(self) -> Sequence[AgentMiddleware]:
        return ()

    def bootstrap_store(self, store: Any) -> None:
        return None

    def default_identity(self) -> Optional[AuthScope]:
        return None

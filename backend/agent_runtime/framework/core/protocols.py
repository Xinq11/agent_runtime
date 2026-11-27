"""Public core extension protocols for the `agent_runtime` Harness SDK.

Downstream customer apps implement these minimal protocols (`SkillProvider`, `RagProvider`,
`LLMProvider`, `AgentMiddleware`, `AgentPlugin`) while owning their own storage, retrieval
engine, and business-level authorization rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
        "通用 Agent Harness SDK 底座：支持流式对话与随时中止、云原生文件读写、上下文预算压缩、子 Agent 委派、高危审批恢复与全链路 Trace 观测。"
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


@dataclass(frozen=True)
class SkillMeta:
    """Lightweight Skill catalog entry returned by a customer `SkillProvider`."""

    slug: str
    description: str = ""
    tags: Tuple[str, ...] = ()
    resources: Tuple[str, ...] = ()


@runtime_checkable
class SkillProvider(Protocol):
    """Customer-implemented hook for listing authorized Skills and reading Skill bodies.

    Storage (DB / Git / OSS / Wiki) and business ACL live entirely inside the customer's
    implementation; the SDK wires `list_skills` / `get_skill` tools and Layer-1 progressive
    disclosure automatically.
    """

    def list_skills(self, scope: AuthScope, query: str = "") -> Any: ...

    def get_skill(self, scope: AuthScope, slug: str, resource: Optional[str] = None) -> Any: ...


@dataclass(frozen=True)
class RagHit:
    """Single grounded retrieval hit returned by a customer `RagProvider`."""

    title: str
    content: str
    section: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class RagResult:
    """Standardized RAG output separating grounded hits, low-confidence, no-hit, and errors."""

    status: str = "ok"  # ok | no_hit | low_confidence | error
    hits: Tuple[RagHit, ...] = ()
    formatted_context: str = ""
    error: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hits", tuple(self.hits or ()))


@runtime_checkable
class RagProvider(Protocol):
    """Customer-implemented hook for RAG retrieval (Milvus / ES / Dify / pgvector / enterprise KB).

    Retrieval pipeline, vector storage, and document-level ACL live entirely inside the
    customer's implementation; the SDK wires `search_knowledge` (and optional budget-bounded
    auto-injection) with grounded citation formatting automatically.
    """

    def retrieve(
        self,
        *,
        scope: AuthScope,
        query: str,
        top_k: int = 4,
        ctx: Optional[ExecutionContext] = None,
    ) -> Any: ...


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
    """Base class for modular customer delivery packs.

    Plugins contribute business tools, declarative read-only sub-agents (`subagents()`),
    optional `SkillProvider` / `RagProvider` hooks, middlewares, prompt segments,
    optional HTTP routers (`routers()`), storage bootstrapping, and UI metadata.
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

    def skill_provider(self) -> Optional[SkillProvider]:
        """Optionally return a customer `SkillProvider` wired into `list_skills`/`get_skill`."""
        return None

    def rag_provider(self) -> Optional[RagProvider]:
        """Optionally return a customer `RagProvider` wired into `search_knowledge`."""
        return None

    def middlewares(self) -> Sequence[AgentMiddleware]:
        return ()

    def routers(self) -> Sequence[Any]:
        """Optionally return FastAPI `APIRouter` instances mounted when running the HTTP server."""
        return ()

    def bootstrap_store(self, store: Any) -> None:
        return None

    def default_identity(self) -> Optional[AuthScope]:
        return None

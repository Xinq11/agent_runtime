"""`agent_runtime` — Pure Internal General Agent Harness SDK.

Provides execution loop, context compaction, tool gateway, declarative sub-agents,
approval pause/resume, cloud workspace files, observability, and thin `SkillProvider`
/ `RagProvider` hooks. Storage, retrieval engines, and business ACL live in customer plugins.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.framework.core.contracts import (
    PROTOCOL_VERSION,
    ApprovalDecision,
    AuthScope,
    ExecutionContext,
    Message,
    ReconcileResult,
    RiskLevel,
    RunRecord,
    SpanRecord,
    SSEEvent,
    ToolCall,
    ToolResult,
    WorkspaceFileRecord,
)
from agent_runtime.framework.core.middleware import MiddlewareChain
from agent_runtime.framework.core.protocols import (
    AgentMiddleware,
    AgentPlugin,
    AppMetadata,
    LLMProvider,
    QuickPromptSpec,
    RagHit,
    RagProvider,
    RagResult,
    SkillMeta,
    SkillProvider,
)
from agent_runtime.framework.core.security import (
    AccessControlError,
    AccessController,
    AccessDecision,
    AuthenticationError,
    AuthorizationError,
    HeaderIdentityResolver,
    IdentityResolver,
    NoopAccessController,
    RbacAccessController,
    ResourceRef,
    StaticIdentityResolver,
    load_access_controller,
    load_identity_resolver,
)

__version__ = "1.0.0"

__all__ = [
    "PROTOCOL_VERSION",
    "AccessControlError",
    "AccessController",
    "AccessDecision",
    "AgentMiddleware",
    "AgentPlugin",
    "AgentRuntimeApp",
    "AppMetadata",
    "ApprovalDecision",
    "AuthScope",
    "AuthenticationError",
    "AuthorizationError",
    "ExecutionContext",
    "HeaderIdentityResolver",
    "IdentityResolver",
    "LLMProvider",
    "Message",
    "MiddlewareChain",
    "NoopAccessController",
    "PgOssStore",
    "QuickPromptSpec",
    "RagHit",
    "RagProvider",
    "RagResult",
    "RbacAccessController",
    "ReconcileResult",
    "ResourceRef",
    "RiskLevel",
    "RunRecord",
    "SkillMeta",
    "SkillProvider",
    "SpanRecord",
    "SSEEvent",
    "StaticIdentityResolver",
    "SubAgentProfile",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "WorkspaceFileRecord",
    "load_access_controller",
    "load_extension",
    "load_identity_resolver",
    "tool",
]


def __getattr__(name: str) -> Any:
    if name in ("AgentRuntimeApp", "load_extension"):
        from agent_runtime.framework.agent import app as _app

        return getattr(_app, name)
    if name == "tool":
        from agent_runtime.framework.tools.decorator import tool as _tool

        return _tool
    if name == "ToolSpec":
        from agent_runtime.framework.tools.registry import ToolSpec as _spec

        return _spec
    if name == "SubAgentProfile":
        from agent_runtime.framework.tools.subagent import SubAgentProfile as _profile

        return _profile
    if name == "PgOssStore":
        from agent_runtime.infra.pg.postgres import PgOssStore as _pg

        return _pg
    raise AttributeError(f"module 'agent_runtime' has no attribute {name!r}")

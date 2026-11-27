"""`agent_runtime` — Unified Internal General Agent SDK.

Core SDK requires ZERO pgvector, RAG, Skills, or Ops tables/routes.
Optional official extensions (`RagExtension`, `SkillsExtension`) are lazily exported here
for convenience without creating any reverse dependency from core `framework/` to `extensions/`.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.framework.core.contracts import (
    PROTOCOL_VERSION,
    ApprovalDecision,
    AuthScope,
    ExecutionContext,
    Message,
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
)
from agent_runtime.framework.core.security import (
    AccessControlError,
    AccessController,
    AccessDecision,
    AccessFilter,
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
    "AccessFilter",
    "AgentMiddleware",
    "AgentPlugin",
    "AgentRuntimeApp",
    "AppMetadata",
    "ApprovalDecision",
    "AuthScope",
    "AuthenticationError",
    "AuthorizationError",
    "ChunkSearchResult",
    "EmbeddingClient",
    "ExecutionContext",
    "HeaderIdentityResolver",
    "IdentityResolver",
    "LLMProvider",
    "Message",
    "MiddlewareChain",
    "NoopAccessController",
    "ParentEvidenceResult",
    "PgOssStore",
    "PgRagRepository",
    "PgSkillsRepository",
    "QuickPromptSpec",
    "RAGChunk",
    "RAGStrategy",
    "RagExtension",
    "RagRetrievalService",
    "RbacAccessController",
    "RerankClient",
    "RerankItem",
    "ResourceRef",
    "RetrievalResult",
    "RiskLevel",
    "RunRecord",
    "SkillSummary",
    "SkillsExtension",
    "SpanRecord",
    "SSEEvent",
    "StaticIdentityResolver",
    "SubAgentProfile",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "WorkspaceFileRecord",
    "diversify_candidates",
    "final_rank_fuse",
    "load_access_controller",
    "load_extension",
    "load_identity_resolver",
    "rerank_candidates",
    "rrf_fuse",
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
    if name == "RagRetrievalService":
        from agent_runtime.extensions.rag.retrieval import RagRetrievalService as _rag

        return _rag
    if name == "PgRagRepository":
        from agent_runtime.extensions.rag.repo import PgRagRepository as _rag_repo

        return _rag_repo
    if name in ("rrf_fuse", "diversify_candidates", "rerank_candidates", "final_rank_fuse"):
        from agent_runtime.extensions.rag import ranking as _rank

        return getattr(_rank, name)
    if name in (
        "RAGChunk",
        "RAGStrategy",
        "RetrievalResult",
        "ChunkSearchResult",
        "ParentEvidenceResult",
        "EmbeddingClient",
        "RerankClient",
        "RerankItem",
    ):
        from agent_runtime.extensions.rag import models as _rm

        return getattr(_rm, name)
    if name == "RagExtension":
        from agent_runtime.extensions.rag.extension import RagExtension as _ext

        return _ext
    if name == "SkillsExtension":
        from agent_runtime.extensions.skills.extension import SkillsExtension as _skills

        return _skills
    if name == "PgSkillsRepository":
        from agent_runtime.extensions.skills.repo import PgSkillsRepository as _skills_repo

        return _skills_repo
    if name == "SkillSummary":
        from agent_runtime.extensions.skills.models import SkillSummary as _skill_summary

        return _skill_summary
    if name == "PgOssStore":
        from agent_runtime.infra.pg.postgres import PgOssStore as _pg

        return _pg
    raise AttributeError(f"module 'agent_runtime' has no attribute {name!r}")

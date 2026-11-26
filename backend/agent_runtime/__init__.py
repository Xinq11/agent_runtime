"""`agent_runtime` — Unified Internal General Agent SDK.

Stable public import surface:
    from agent_runtime import (
        BaseAgent,
        AgentRuntimeApp,
        AgentPlugin,
        AgentMiddleware,
        RAGStrategy,
        RagExtension,
        RagRetrievalService,
        SkillsExtension,
        tool,
        ToolSpec,
        SubAgentProfile,
        AuthScope,
        ExecutionContext,
        RiskLevel,
    )
"""

from __future__ import annotations

from typing import Any

from agent_runtime.framework.core.contracts import (
    PROTOCOL_VERSION,
    ApprovalDecision,
    AuthScope,
    ExecutionContext,
    Message,
    RAGChunk,
    RetrievalResult,
    RiskLevel,
    RunRecord,
    SkillSummary,
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
    RAGStrategy,
)

__version__ = "1.0.0"

__all__ = [
    "PROTOCOL_VERSION",
    "AgentMiddleware",
    "AgentPlugin",
    "AgentRuntimeApp",
    "AppMetadata",
    "ApprovalDecision",
    "AuthScope",
    "BaseAgent",
    "ChunkSearchResult",
    "EmbeddingClient",
    "ExecutionContext",
    "LLMProvider",
    "Message",
    "MiddlewareChain",
    "ParentEvidenceResult",
    "QuickPromptSpec",
    "RAGChunk",
    "RAGStrategy",
    "RagExtension",
    "RagRetrievalService",
    "RerankClient",
    "RerankItem",
    "RetrievalResult",
    "RiskLevel",
    "RunRecord",
    "SkillSummary",
    "SkillsExtension",
    "SpanRecord",
    "SSEEvent",
    "SubAgentProfile",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "WorkspaceFileRecord",
    "diversify_candidates",
    "final_rank_fuse",
    "load_extension",
    "rerank_candidates",
    "rrf_fuse",
    "tool",
]


def __getattr__(name: str) -> Any:
    if name in ("AgentRuntimeApp", "load_extension"):
        from agent_runtime.framework.agent import app as _app

        return getattr(_app, name)
    if name == "BaseAgent":
        from agent_runtime.framework.agent.base import BaseAgent as _base

        return _base
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
    if name in ("rrf_fuse", "diversify_candidates", "rerank_candidates", "final_rank_fuse"):
        from agent_runtime.extensions.rag import ranking as _rank

        return getattr(_rank, name)
    if name in ("ChunkSearchResult", "ParentEvidenceResult", "EmbeddingClient", "RerankClient", "RerankItem"):
        from agent_runtime.extensions.rag import models as _rm

        return getattr(_rm, name)
    if name == "RagExtension":
        from agent_runtime.extensions.rag.extension import RagExtension as _ext

        return _ext
    if name == "SkillsExtension":
        from agent_runtime.extensions.skills.extension import SkillsExtension as _skills

        return _skills
    raise AttributeError(f"module 'agent_runtime' has no attribute {name!r}")

"""Shared data contracts for the `agent_runtime` SDK (grouped by domain)."""

from agent_runtime.framework.core.contracts.artifacts import (
    RAGChunk,
    RetrievalResult,
    SkillSummary,
    ToolOutputBlob,
    WorkspaceFileRecord,
)
from agent_runtime.framework.core.contracts.identity import (
    PROTOCOL_VERSION,
    AuthScope,
    ExecutionContext,
    ProgressCallback,
    RiskLevel,
    SSEEvent,
)
from agent_runtime.framework.core.contracts.messages import (
    Message,
    ToolCall,
    ToolResult,
    canonical_hash,
    estimate_tokens,
)
from agent_runtime.framework.core.contracts.runtime import (
    ApprovalDecision,
    ApprovalTicket,
    RunRecord,
    SpanRecord,
    ToolRunRecord,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ApprovalDecision",
    "ApprovalTicket",
    "AuthScope",
    "ExecutionContext",
    "Message",
    "ProgressCallback",
    "RAGChunk",
    "RetrievalResult",
    "RiskLevel",
    "RunRecord",
    "SkillSummary",
    "SpanRecord",
    "SSEEvent",
    "ToolCall",
    "ToolOutputBlob",
    "ToolResult",
    "ToolRunRecord",
    "WorkspaceFileRecord",
    "canonical_hash",
    "estimate_tokens",
]

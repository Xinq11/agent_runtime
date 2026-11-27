"""RAG domain contracts, per-stage retrieval models, and pluggable strategy/client protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from agent_runtime.framework.core.contracts import AuthScope, ExecutionContext


@dataclass
class RAGChunk:
    chunk_id: str
    parent_id: str
    doc_title: str
    section_path: str
    doc_url: str
    content: str
    parent_text: str
    acl_roles: Tuple[str, ...]
    status: str = "active"
    embedding: Tuple[float, ...] = ()
    score: float = 0.0


@dataclass
class RetrievalResult:
    """Standardized RAG output separating grounded hits, low-confidence, no-hit, and errors."""

    status: str  # ok | no_hit | low_confidence | error | acl_error
    hits: List[RAGChunk] = field(default_factory=list)
    formatted_context: str = ""
    rewritten_query: Optional[str] = None
    corrected: bool = False
    error: Optional[str] = None


@runtime_checkable
class RAGStrategy(Protocol):
    """Protocol for pluggable custom RAG strategies (built-in 7-stage RAG, Milvus, ES, Dify, etc.)."""

    async def retrieve(
        self,
        *,
        scope: AuthScope,
        query: str,
        top_k: int = 4,
        ctx: Optional[ExecutionContext] = None,
    ) -> RetrievalResult: ...


@dataclass(frozen=True)
class SparseSearchResult:
    chunk: RAGChunk
    sparse_score: float


@dataclass(frozen=True)
class DenseSearchResult:
    chunk: RAGChunk
    dense_score: float


@dataclass(frozen=True)
class RrfFusionResult:
    chunk: RAGChunk
    rrf_score: float
    best_sparse_score: Optional[float]
    best_dense_score: Optional[float]


@dataclass(frozen=True)
class ChunkSearchResult:
    """Public child-level diagnostic result carrying all 4 distinct stage scores."""

    chunk_id: str
    parent_id: str
    chunk_ordinal: int
    doc_title: str
    section_path: str
    doc_url: str
    content: str
    parent_text: str
    sparse_score: Optional[float]
    dense_score: Optional[float]
    rrf_score: float
    rerank_score: float


@dataclass(frozen=True)
class ParentEvidenceResult:
    """Parent-level grounded evidence passed into LLM context (raw retrieval scores omitted)."""

    parent_id: str
    doc_title: str
    section_path: str
    doc_url: str
    content: str
    matched_child_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RerankItem:
    index: int
    score: float


@runtime_checkable
class EmbeddingClient(Protocol):
    def embed(self, text: str) -> Tuple[float, ...]: ...


@runtime_checkable
class RerankClient(Protocol):
    def rerank(self, query: str, documents: Sequence[str]) -> List[RerankItem]: ...

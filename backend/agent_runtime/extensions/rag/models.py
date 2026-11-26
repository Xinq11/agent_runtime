"""Strict per-stage CRAG retrieval result models and pluggable Embedding/Rerank client protocols.

Mirrors the ChatBI / CRAG retrieval style contract:
  - Each stage owns an explicit result type (never overload one generic `score` field).
  - `retrieve()` returns child-level `ChunkSearchResult` (all 4 stage scores for eval/diagnostics).
  - `retrieve_evidence()` returns parent-level `ParentEvidenceResult` (NO raw scores leaked to LLM).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from agent_runtime.framework.core.contracts import RAGChunk


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

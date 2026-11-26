"""Stateless CRAG ranking functions: RRF fusion, per-doc diversification, rerank, final fusion."""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Sequence, Set, Tuple

from agent_runtime.extensions.rag.models import (
    ChunkSearchResult,
    DenseSearchResult,
    RerankClient,
    RrfFusionResult,
    SparseSearchResult,
)
from agent_runtime.framework.core.contracts import RAGChunk

log = logging.getLogger("agent-runtime.crag")

RRF_K = 60
RERANK_MAX_DOCUMENT_CHARS = 256
MAX_RERANK_CANDIDATES_PER_DOCUMENT = 2
FINAL_RANK_CONSTANT = 60
FINAL_RETRIEVAL_WEIGHT = 0.8
_ORDINAL_RE = re.compile(r"_(\d+)$")


def infer_chunk_ordinal(chunk_id: str) -> int:
    m = _ORDINAL_RE.search(chunk_id or "")
    return int(m.group(1)) if m else 0


def rrf_fuse(
    sparse_results: Sequence[SparseSearchResult],
    dense_results: Sequence[DenseSearchResult],
    top_n: int,
) -> List[RrfFusionResult]:
    """Reciprocal Rank Fusion (RRF_K=60, deterministic chunk_id tie-break)."""
    if top_n <= 0:
        return []
    rrf_scores: Dict[str, float] = {}
    chunks: Dict[str, RAGChunk] = {}
    sparse_scores: Dict[str, float] = {}
    dense_scores: Dict[str, float] = {}

    for idx, r in enumerate(sparse_results or ()):
        cid = r.chunk.chunk_id
        chunks.setdefault(cid, r.chunk)
        sparse_scores[cid] = max(sparse_scores.get(cid, float("-inf")), r.sparse_score)
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (RRF_K + idx + 1)

    for idx, r in enumerate(dense_results or ()):
        cid = r.chunk.chunk_id
        chunks.setdefault(cid, r.chunk)
        dense_scores[cid] = max(dense_scores.get(cid, float("-inf")), r.dense_score)
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (RRF_K + idx + 1)

    ranked_ids = sorted(rrf_scores.keys(), key=lambda cid: (-rrf_scores[cid], cid))[:top_n]
    return [
        RrfFusionResult(
            chunk=chunks[cid],
            rrf_score=rrf_scores[cid],
            best_sparse_score=sparse_scores.get(cid),
            best_dense_score=dense_scores.get(cid),
        )
        for cid in ranked_ids
    ]


def _doc_key(chunk: RAGChunk) -> str:
    return chunk.doc_url or f"{chunk.doc_title}:{chunk.section_path}"


def diversify_candidates(
    candidates: Sequence[RrfFusionResult],
    limit: int,
    max_per_document: int = MAX_RERANK_CANDIDATES_PER_DOCUMENT,
) -> List[RrfFusionResult]:
    """Two-pass selection capping per-document candidates before reranking."""
    if not candidates or limit <= 0 or max_per_document <= 0:
        return []
    selected: List[RrfFusionResult] = []
    deferred: List[RrfFusionResult] = []
    doc_counts: Dict[str, int] = {}
    seen_parents: Set[str] = set()
    seen_chunks: Set[str] = set()

    for cand in candidates:
        dkey = _doc_key(cand.chunk)
        if doc_counts.get(dkey, 0) >= max_per_document or cand.chunk.parent_id in seen_parents:
            deferred.append(cand)
            continue
        selected.append(cand)
        seen_parents.add(cand.chunk.parent_id)
        seen_chunks.add(cand.chunk.chunk_id)
        doc_counts[dkey] = doc_counts.get(dkey, 0) + 1
        if len(selected) == limit:
            return selected

    for cand in deferred:
        dkey = _doc_key(cand.chunk)
        if doc_counts.get(dkey, 0) >= max_per_document or cand.chunk.chunk_id in seen_chunks:
            continue
        selected.append(cand)
        seen_chunks.add(cand.chunk.chunk_id)
        doc_counts[dkey] = doc_counts.get(dkey, 0) + 1
        if len(selected) == limit:
            break
    return selected


def _to_chunk_result(cand: RrfFusionResult, rerank_score: float) -> ChunkSearchResult:
    c = cand.chunk
    return ChunkSearchResult(
        chunk_id=c.chunk_id,
        parent_id=c.parent_id,
        chunk_ordinal=infer_chunk_ordinal(c.chunk_id),
        doc_title=c.doc_title,
        section_path=c.section_path,
        doc_url=c.doc_url,
        content=c.content,
        parent_text=c.parent_text,
        sparse_score=cand.best_sparse_score,
        dense_score=cand.best_dense_score,
        rrf_score=cand.rrf_score,
        rerank_score=rerank_score,
    )


def rerank_candidates(
    rerank_client: RerankClient,
    query: str,
    candidates: Sequence[RrfFusionResult],
    max_candidates: int,
    max_document_chars: int = RERANK_MAX_DOCUMENT_CHARS,
) -> List[ChunkSearchResult]:
    """Rerank RRF candidates via `rerank_client`, falling back to RRF order on error."""
    if not query or not query.strip() or not candidates or max_candidates <= 0:
        return []
    selected = list(candidates[:max_candidates])
    docs = [
        f"标题：{c.chunk.doc_title}\n路径：{c.chunk.section_path}\n正文：{c.chunk.content}"[:max_document_chars]
        for c in selected
    ]
    try:
        items = rerank_client.rerank(query, docs)
    except Exception as exc:
        log.warning("rerank failed, falling back to RRF order: %s", type(exc).__name__)
        return [_to_chunk_result(c, 0.0) for c in selected]
    if not items:
        return [_to_chunk_result(c, 0.0) for c in selected]

    scores = [0.0] * len(selected)
    for item in items:
        if 0 <= item.index < len(scores):
            scores[item.index] = float(item.score)

    indexed = list(enumerate(zip(selected, scores)))
    indexed.sort(key=lambda pair: (-pair[1][1], pair[0]))
    return [_to_chunk_result(cand, sc) for _, (cand, sc) in indexed]


def final_rank_fuse(
    fused_candidates: Sequence[RrfFusionResult],
    reranked: Sequence[ChunkSearchResult],
) -> List[ChunkSearchResult]:
    """Rank-level fusion retaining retrieval rank evidence after reranking (k=60, w=0.8)."""
    if not reranked:
        return []
    retrieval_ranks: Dict[str, int] = {}
    for idx, cand in enumerate(fused_candidates):
        retrieval_ranks.setdefault(cand.chunk.chunk_id, idx + 1)

    fallback_rank = len(fused_candidates) + 1
    scored: List[Tuple[float, int, ChunkSearchResult]] = []
    for idx, res in enumerate(reranked):
        r_rank = retrieval_ranks.get(res.chunk_id, fallback_rank)
        final_score = (1.0 / (FINAL_RANK_CONSTANT + idx + 1)) + FINAL_RETRIEVAL_WEIGHT * (
            1.0 / (FINAL_RANK_CONSTANT + r_rank)
        )
        scored.append((final_score, idx, res))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored]

"""Parent-evidence aggregation and score-free LLM citation formatting."""

from __future__ import annotations

from typing import Dict, List, Sequence, Set, Tuple

from agent_runtime.extensions.rag.models import ChunkSearchResult, ParentEvidenceResult


def aggregate_parent_evidences(
    final_children: Sequence[ChunkSearchResult],
    real_rrf_hits: Set[str],
    *,
    min_rerank_score: float,
    top_n: int,
) -> Tuple[List[ParentEvidenceResult], float]:
    """Aggregate real RRF child hits into score-free parent evidence blocks."""
    if not final_children:
        return [], 0.0

    top_score = max(c.rerank_score for c in final_children)
    best_rank: Dict[str, int] = {}
    matched_children: Dict[str, List[str]] = {}
    meta_by_parent: Dict[str, ChunkSearchResult] = {}

    for rank_idx, child in enumerate(final_children):
        if child.rerank_score < min_rerank_score:
            continue
        pid = child.parent_id
        if not pid:
            continue
        best_rank.setdefault(pid, rank_idx)
        meta_by_parent.setdefault(pid, child)
        if child.chunk_id in real_rrf_hits:
            lst = matched_children.setdefault(pid, [])
            if child.chunk_id not in lst:
                lst.append(child.chunk_id)

    ordered_pids = sorted(matched_children.keys(), key=lambda p: best_rank[p])[:top_n]
    evidences: List[ParentEvidenceResult] = []
    for pid in ordered_pids:
        meta = meta_by_parent[pid]
        content = (meta.parent_text or meta.content or "").strip()
        if not content:
            continue
        evidences.append(
            ParentEvidenceResult(
                parent_id=pid,
                doc_title=meta.doc_title,
                section_path=meta.section_path,
                doc_url=meta.doc_url,
                content=content,
                matched_child_ids=tuple(matched_children[pid]),
            )
        )
    return evidences, top_score


def format_parent_evidences(evidences: Sequence[ParentEvidenceResult]) -> str:
    """Format score-free parent evidences into numbered [1], [2]... LLM context blocks."""
    return "\n\n".join(
        f"[{i}] 《{ev.doc_title}》 - {ev.section_path} (来源: {ev.doc_url})\n{ev.content}"
        for i, ev in enumerate(evidences, 1)
    )

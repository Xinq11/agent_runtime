"""Similarity and citation helpers for the standard RAG extension."""
from __future__ import annotations

from typing import Iterable, List, Optional

from agent_runtime.extensions._search_text import VECTOR_DIM, chunk_text, embed_text, terms
from agent_runtime.extensions.rag.models import RAGChunk


def cosine(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def format_grounded_context(hits: List[RAGChunk], *, rewritten_query: Optional[str] = None) -> str:
    blocks = [
        f"[{i}] 《{h.doc_title}》 - {h.section_path} (来源: {h.doc_url})\n{h.parent_text}"
        for i, h in enumerate(hits, 1)
    ]
    return "\n\n".join(blocks)

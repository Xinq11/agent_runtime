"""In-memory sparse/dense/adjacent RAG repository mixin."""

from __future__ import annotations

from copy import deepcopy
from typing import List, Tuple

from agent_runtime.extensions.rag.text_utils import chunk_text, cosine, terms
from agent_runtime.framework.core.contracts import AuthScope, RAGChunk


class MemoryRagMixin:
    """In-memory RAG chunk storage and recall ports."""

    def upsert_rag_chunk(self, chunk: RAGChunk) -> None:
        with self._lock:
            self._chunks[(chunk.tenant_id, chunk.chunk_id)] = deepcopy(chunk)

    def _visible_chunks(self, scope: AuthScope) -> List[RAGChunk]:
        roles = set(scope.roles)
        with self._lock:
            return [
                deepcopy(c)
                for (tenant, _), c in self._chunks.items()
                if tenant == scope.tenant_id
                and c.status == "active"
                and (not roles or roles.intersection(c.acl_roles))
            ]

    def search_sparse_rag(
        self, scope: AuthScope, query_terms: List[str], limit: int
    ) -> List[Tuple[RAGChunk, float]]:
        qset = set(query_terms)
        if limit < 1 or not qset:
            return []
        scored: List[Tuple[float, str, RAGChunk]] = []
        for c in self._visible_chunks(scope):
            words = set(terms(chunk_text(c)))
            overlap = len(qset & words)
            if overlap:
                scored.append((overlap / (len(words) ** 0.5), c.chunk_id, c))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [(c, sc) for sc, _, c in scored[:limit]]

    def search_dense_rag(
        self, scope: AuthScope, query_vec, limit: int
    ) -> List[Tuple[RAGChunk, float]]:
        if limit < 1:
            return []
        scored: List[Tuple[float, str, RAGChunk]] = []
        for c in self._visible_chunks(scope):
            sim = cosine(query_vec, c.embedding)
            if sim > 0:
                scored.append((sim, c.chunk_id, c))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [(c, sc) for sc, _, c in scored[:limit]]

    def find_adjacent_rag_chunks(self, scope: AuthScope, parent_ids: List[str]) -> List[RAGChunk]:
        pset = set(parent_ids)
        if not pset:
            return []
        out = [c for c in self._visible_chunks(scope) if c.parent_id in pset]
        return sorted(out, key=lambda c: c.chunk_id)

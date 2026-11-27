"""RAG 7-stage retrieval service orchestrating recall, fusion, diversification, rerank, and evidence."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import logging
from typing import Any, List, Optional, Set, Tuple

from agent_runtime.extensions.rag.clients import LocalHashEmbeddingClient, LocalLexicalRerankClient
from agent_runtime.extensions.rag.evidence import aggregate_parent_evidences, format_parent_evidences
from agent_runtime.extensions.rag.models import (
    ChunkSearchResult,
    DenseSearchResult,
    EmbeddingClient,
    ParentEvidenceResult,
    RAGChunk,
    RerankClient,
    RetrievalResult,
    RrfFusionResult,
    SparseSearchResult,
)
from agent_runtime.extensions.rag.ranking import (
    MAX_RERANK_CANDIDATES_PER_DOCUMENT,
    RERANK_MAX_DOCUMENT_CHARS,
    diversify_candidates,
    final_rank_fuse,
    infer_chunk_ordinal,
    rerank_candidates,
    rrf_fuse,
)
from agent_runtime.extensions.rag.repo import PgRagRepository
from agent_runtime.extensions.rag.text_utils import chunk_text, terms
from agent_runtime.framework.core.contracts import AuthScope, ExecutionContext
from agent_runtime.framework.core.security import (
    AccessControlError,
    AccessController,
    AccessFilter,
    enforce_filter,
    enforce_filter_sync,
)

log = logging.getLogger("agent-runtime.rag")


def _as_rag_repo(store_or_repo: Any) -> Any:
    if hasattr(store_or_repo, "search_sparse_rag") and hasattr(store_or_repo, "search_dense_rag"):
        return store_or_repo
    return PgRagRepository(store_or_repo)


class RagRetrievalService:
    """Full 7-stage RAG retrieval facade implementing `RAGStrategy`."""

    def __init__(
        self,
        store: Any,
        *,
        embedding_client: Optional[EmbeddingClient] = None,
        rerank_client: Optional[RerankClient] = None,
        min_rerank_score: float = 0.35,
        max_per_document: int = MAX_RERANK_CANDIDATES_PER_DOCUMENT,
        rerank_max_chars: int = RERANK_MAX_DOCUMENT_CHARS,
        access_controller: Optional[AccessController] = None,
    ) -> None:
        self.store = _as_rag_repo(store)
        self.embedding_client = embedding_client or LocalHashEmbeddingClient()
        self.rerank_client = rerank_client or LocalLexicalRerankClient()
        self.min_rerank_score = min_rerank_score
        self.max_per_document = max_per_document
        self.rerank_max_chars = rerank_max_chars
        self.access_controller = access_controller

    def index_chunk(self, chunk: RAGChunk) -> None:
        vec = self.embedding_client.embed(chunk_text(chunk))
        self.store.upsert_rag_chunk(replace(chunk, embedding=vec))

    def _run_pipeline(
        self,
        scope: AuthScope,
        query: str,
        top_n: int,
        *,
        access_filter: Optional[AccessFilter] = None,
    ) -> Tuple[List[ChunkSearchResult], Set[str]]:
        if not query or not query.strip() or top_n <= 0:
            return [], set()

        flt = access_filter
        if flt is None and self.access_controller is not None:
            flt = enforce_filter_sync(self.access_controller, scope, "kb:retrieve", "rag_doc")

        rerank_n = max(1, top_n * 2)
        recall_n = max(1, rerank_n * 3)

        try:
            query_vec = self.embedding_client.embed(query)
        except Exception as exc:
            log.warning("embedding failed, skipping RAG retrieval: %s", type(exc).__name__)
            return [], set()

        query_terms = sorted(set(terms(query)))
        if not query_terms:
            return [], set()

        sparse_results = [
            SparseSearchResult(c, s)
            for c, s in self.store.search_sparse_rag(scope, query_terms, recall_n, access_filter=flt)
        ]
        dense_results = [
            DenseSearchResult(c, s)
            for c, s in self.store.search_dense_rag(scope, query_vec, recall_n, access_filter=flt)
        ]

        fused = rrf_fuse(sparse_results, dense_results, max(rerank_n, rerank_n * 2))
        if not fused:
            return [], set()

        candidates_map: "OrderedDict[str, RrfFusionResult]" = OrderedDict()
        real_rrf_hits: Set[str] = set()
        adjacent_keys: Set[Tuple[str, int]] = set()
        parent_ids: List[str] = []

        for item in fused:
            cid = item.chunk.chunk_id
            candidates_map.setdefault(cid, item)
            real_rrf_hits.add(cid)
            pid = item.chunk.parent_id
            if pid:
                if pid not in parent_ids:
                    parent_ids.append(pid)
                ord_num = infer_chunk_ordinal(cid)
                if ord_num > 1:
                    adjacent_keys.add((pid, ord_num - 1))
                if ord_num > 0:
                    adjacent_keys.add((pid, ord_num + 1))

        if adjacent_keys and hasattr(self.store, "find_adjacent_rag_chunks"):
            for sibling in self.store.find_adjacent_rag_chunks(scope, parent_ids, access_filter=flt):
                key = (sibling.parent_id, infer_chunk_ordinal(sibling.chunk_id))
                if key in adjacent_keys and sibling.chunk_id not in candidates_map:
                    candidates_map[sibling.chunk_id] = RrfFusionResult(
                        chunk=sibling, rrf_score=0.0, best_sparse_score=None, best_dense_score=None
                    )

        selected = diversify_candidates(
            list(candidates_map.values()), rerank_n, self.max_per_document
        )
        reranked = rerank_candidates(
            self.rerank_client, query, selected, rerank_n, self.rerank_max_chars
        )
        final_children = final_rank_fuse(selected, reranked)[:rerank_n]
        return final_children, real_rrf_hits

    def retrieve_chunks(
        self,
        scope: AuthScope,
        query: str,
        top_n: int = 4,
        *,
        access_filter: Optional[AccessFilter] = None,
    ) -> List[ChunkSearchResult]:
        final_children, _ = self._run_pipeline(scope, query, top_n, access_filter=access_filter)
        return final_children[:top_n]

    def retrieve_evidence(
        self,
        scope: AuthScope,
        query: str,
        top_n: int = 4,
        *,
        access_filter: Optional[AccessFilter] = None,
    ) -> Tuple[List[ParentEvidenceResult], float]:
        final_children, real_rrf_hits = self._run_pipeline(
            scope, query, top_n, access_filter=access_filter
        )
        return aggregate_parent_evidences(
            final_children,
            real_rrf_hits,
            min_rerank_score=self.min_rerank_score,
            top_n=top_n,
        )

    async def retrieve(
        self,
        *,
        scope: AuthScope,
        query: str,
        top_k: int = 4,
        ctx: Optional[ExecutionContext] = None,
    ) -> RetrievalResult:
        flt: Optional[AccessFilter] = None
        if self.access_controller is not None:
            try:
                flt = await enforce_filter(self.access_controller, scope, "kb:retrieve", "rag_doc")
            except AccessControlError as exc:
                return RetrievalResult(
                    status="acl_error",
                    error=str(exc),
                    formatted_context="[ACL_ERROR] 权限服务暂不可用，已拒绝本次知识检索。",
                )
        try:
            evidences, top_score = self.retrieve_evidence(scope, query, top_n=top_k, access_filter=flt)
        except AccessControlError as exc:
            return RetrievalResult(
                status="acl_error",
                error=str(exc),
                formatted_context="[ACL_ERROR] 权限服务暂不可用，已拒绝本次知识检索。",
            )
        except Exception as exc:
            return RetrievalResult(status="error", error=type(exc).__name__)

        if not evidences:
            if top_score > 0:
                return RetrievalResult(
                    status="low_confidence",
                    formatted_context=(
                        f"[LOW_CONFIDENCE] 最高重排相关度 {top_score:.2f} 低于阈值 {self.min_rerank_score:.2f}，"
                        "未找到足够依据，请诚实告知用户资料缺失。"
                    ),
                )
            return RetrievalResult(
                status="no_hit",
                formatted_context="[NO_HIT] 知识库中未检索到相关文档，请勿编造结论。",
            )

        hits = [
            RAGChunk(
                chunk_id=ev.matched_child_ids[0] if ev.matched_child_ids else ev.parent_id,
                parent_id=ev.parent_id,
                doc_title=ev.doc_title,
                section_path=ev.section_path,
                doc_url=ev.doc_url,
                content=ev.content,
                parent_text=ev.content,
                acl_roles=scope.roles,
                score=0.0,
            )
            for ev in evidences
        ]
        return RetrievalResult(
            status="ok",
            hits=hits,
            formatted_context=format_parent_evidences(evidences),
        )

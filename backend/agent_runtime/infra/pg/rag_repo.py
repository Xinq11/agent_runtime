"""PostgreSQL + pgvector + OSS repository mixin for RAG indexing and dual-channel recall."""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

from agent_runtime.extensions.rag.text_utils import chunk_text, embed_text, terms
from agent_runtime.framework.core.config import BUCKET, uid
from agent_runtime.framework.core.contracts import AuthScope, RAGChunk
from agent_runtime.framework.core.security import AccessFilter
from agent_runtime.infra.pg.acl_sql import compile_acl_sql, default_access_filter


class PgRagMixin:
    """Chunk/document persistence and sparse/dense/adjacent recall for RAG."""

    def upsert_rag_chunk(self, chunk: RAGChunk) -> None:
        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO rag_parents (tenant_id,parent_id,doc_title,section_path,doc_url,parent_text)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,parent_id) DO UPDATE SET
                   doc_title=EXCLUDED.doc_title,section_path=EXCLUDED.section_path,
                   doc_url=EXCLUDED.doc_url,parent_text=EXCLUDED.parent_text""",
                (
                    chunk.tenant_id,
                    chunk.parent_id,
                    chunk.doc_title,
                    chunk.section_path,
                    chunk.doc_url,
                    chunk.parent_text,
                ),
            )
            cur.execute(
                """INSERT INTO rag_chunks (tenant_id,chunk_id,parent_id,content,acl_roles,status,embedding,fts_terms)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s)
                   ON CONFLICT (tenant_id,chunk_id) DO UPDATE SET parent_id=EXCLUDED.parent_id,
                   content=EXCLUDED.content,acl_roles=EXCLUDED.acl_roles,status=EXCLUDED.status,
                   embedding=EXCLUDED.embedding,fts_terms=EXCLUDED.fts_terms""",
                (
                    chunk.tenant_id,
                    chunk.chunk_id,
                    chunk.parent_id,
                    chunk.content,
                    list(chunk.acl_roles),
                    chunk.status,
                    json.dumps(list(chunk.embedding)),
                    " ".join(terms(chunk_text(chunk))),
                ),
            )

    def search_sparse_rag(
        self,
        scope: AuthScope,
        query_terms: List[str],
        limit: int,
        *,
        access_filter: Optional[AccessFilter] = None,
    ) -> List[Tuple[RAGChunk, float]]:
        words = sorted(set(query_terms))
        if limit <= 0 or not words:
            return []
        flt = access_filter or default_access_filter(scope)
        acl_sql, acl_params = compile_acl_sql(flt, scope=scope, alias="c")
        cx_acl_sql, cx_acl_params = compile_acl_sql(flt, scope=scope, alias="cx", param_prefix="aclx")
        with self.transaction() as cur:
            cur.execute(
                f"""
                WITH q AS (SELECT to_tsquery('simple', %(words)s) AS ts)
                SELECT c.chunk_id,c.parent_id,c.tenant_id,p.doc_title,p.section_path,p.doc_url,
                       c.content,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM rag_chunks cx
                           WHERE cx.tenant_id=c.tenant_id AND cx.parent_id=c.parent_id
                             AND cx.status='active' AND NOT ({cx_acl_sql})
                       ) THEN '' ELSE p.parent_text END AS parent_text,
                       c.acl_roles,c.status,c.embedding::text,
                       ts_rank(c.fts_tsv, q.ts)::float8 AS sparse_score
                FROM rag_chunks c
                JOIN rag_parents p USING (tenant_id, parent_id), q
                WHERE {acl_sql}
                  AND c.fts_tsv @@ q.ts
                ORDER BY sparse_score DESC, c.chunk_id
                LIMIT %(k)s
                """,
                {**acl_params, **cx_acl_params, "words": " | ".join(words), "k": limit},
            )
            return [
                (RAGChunk(*row[:8], tuple(row[8]), row[9], tuple(json.loads(row[10])), row[11]), float(row[11]))
                for row in cur.fetchall()
            ]

    def search_dense_rag(
        self,
        scope: AuthScope,
        query_vec,
        limit: int,
        *,
        access_filter: Optional[AccessFilter] = None,
    ) -> List[Tuple[RAGChunk, float]]:
        if limit <= 0:
            return []
        flt = access_filter or default_access_filter(scope)
        acl_sql, acl_params = compile_acl_sql(flt, scope=scope, alias="c")
        cx_acl_sql, cx_acl_params = compile_acl_sql(flt, scope=scope, alias="cx", param_prefix="aclx")
        with self.transaction() as cur:
            cur.execute(
                f"""
                SELECT c.chunk_id,c.parent_id,c.tenant_id,p.doc_title,p.section_path,p.doc_url,
                       c.content,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM rag_chunks cx
                           WHERE cx.tenant_id=c.tenant_id AND cx.parent_id=c.parent_id
                             AND cx.status='active' AND NOT ({cx_acl_sql})
                       ) THEN '' ELSE p.parent_text END AS parent_text,
                       c.acl_roles,c.status,c.embedding::text,
                       (1.0 - (c.embedding <=> %(vec)s::vector))::float8 AS dense_score
                FROM rag_chunks c
                JOIN rag_parents p USING (tenant_id, parent_id)
                WHERE {acl_sql}
                  AND c.embedding <=> %(vec)s::vector < 1
                ORDER BY c.embedding <=> %(vec)s::vector, c.chunk_id
                LIMIT %(k)s
                """,
                {
                    **acl_params,
                    **cx_acl_params,
                    "vec": json.dumps(list(query_vec)),
                    "k": limit,
                },
            )
            return [
                (RAGChunk(*row[:8], tuple(row[8]), row[9], tuple(json.loads(row[10])), row[11]), float(row[11]))
                for row in cur.fetchall()
            ]

    def find_adjacent_rag_chunks(
        self,
        scope: AuthScope,
        parent_ids: List[str],
        *,
        access_filter: Optional[AccessFilter] = None,
    ) -> List[RAGChunk]:
        if not parent_ids:
            return []
        flt = access_filter or default_access_filter(scope)
        acl_sql, acl_params = compile_acl_sql(flt, scope=scope, alias="c")
        cx_acl_sql, cx_acl_params = compile_acl_sql(flt, scope=scope, alias="cx", param_prefix="aclx")
        with self.transaction() as cur:
            cur.execute(
                f"""
                SELECT c.chunk_id,c.parent_id,c.tenant_id,p.doc_title,p.section_path,p.doc_url,
                       c.content,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM rag_chunks cx
                           WHERE cx.tenant_id=c.tenant_id AND cx.parent_id=c.parent_id
                             AND cx.status='active' AND NOT ({cx_acl_sql})
                       ) THEN '' ELSE p.parent_text END AS parent_text,
                       c.acl_roles,c.status,c.embedding::text
                FROM rag_chunks c
                JOIN rag_parents p USING (tenant_id, parent_id)
                WHERE {acl_sql}
                  AND c.parent_id = ANY(%(parent_ids)s)
                ORDER BY c.chunk_id
                """,
                {**acl_params, **cx_acl_params, "parent_ids": list(parent_ids)},
            )
            return [
                RAGChunk(*row[:8], tuple(row[8]), row[9], tuple(json.loads(row[10])), 0.0)
                for row in cur.fetchall()
            ]

    def index_document(self, scope, title, text, roles, *, parent=None, section=None, upload=False):
        pid = parent or uid("doc")
        key = f"knowledge/{pid}.txt" if upload else None
        if key:
            self.oss.put_text(key, text)
        with self.transaction() as cur:
            url = f"s3://{BUCKET}/{key}" if key else f"kb://{pid}"
            cur.execute(
                """INSERT INTO rag_parents(tenant_id,parent_id,doc_title,section_path,doc_url,parent_text,oss_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING parent_id""",
                (scope.tenant_id, pid, title, section or title, url, "" if upload else text, key),
            )
            if not cur.fetchone():
                return {"parent_id": pid, "oss_key": key, "chunk_count": 0}
            chunks = []
            for i, start in enumerate(range(0, len(text), 700)):
                piece = text[start : start + 800]
                c = RAGChunk(
                    f"{pid}_{i + 1}",
                    pid,
                    scope.tenant_id,
                    title,
                    section or title,
                    url,
                    piece,
                    text,
                    tuple(roles),
                )
                tokens = chunk_text(c)
                chunks.append(
                    (
                        scope.tenant_id,
                        c.chunk_id,
                        pid,
                        piece,
                        list(roles),
                        json.dumps(embed_text(tokens)),
                        " ".join(terms(tokens)),
                    )
                )
                if start + 800 >= len(text):
                    break
            cur.executemany(
                """INSERT INTO rag_chunks(tenant_id,chunk_id,parent_id,content,acl_roles,embedding,fts_terms)
                   VALUES (%s,%s,%s,%s,%s,%s::vector,%s)""",
                chunks,
            )
        return {"parent_id": pid, "doc_title": title, "oss_key": key, "chunk_count": len(chunks)}

    def set_document_status(self, scope: AuthScope, parent_id: str, status: str) -> bool:
        if status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        with self.transaction() as cur:
            cur.execute(
                "UPDATE rag_chunks SET status=%s WHERE tenant_id=%s AND parent_id=%s RETURNING parent_id",
                (status, scope.tenant_id, parent_id),
            )
            return bool(cur.fetchone())

"""Standalone PostgreSQL + pgvector + OSS repository owned by `RagExtension`."""

from __future__ import annotations

from importlib.resources import files
import json
from typing import Any, Dict, List, Optional, Tuple

from agent_runtime.extensions._acl_sql import compile_acl_sql, default_access_filter
from agent_runtime.extensions._search_text import chunk_text, embed_text, terms
from agent_runtime.extensions.rag.models import RAGChunk
from agent_runtime.framework.core.config import BUCKET, uid
from agent_runtime.framework.core.contracts import AuthScope
from agent_runtime.framework.core.security import AccessFilter


class PgRagRepository:
    """Chunk/document persistence and sparse/dense/adjacent recall for RAG."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def bootstrap_schema(self) -> None:
        ddl = files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
        with self.store.transaction() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('agent_runtime.rag.schema'))")
            cur.execute(ddl, prepare=False)

    def upsert_rag_chunk(self, chunk: RAGChunk) -> None:
        with self.store.transaction() as cur:
            cur.execute(
                """INSERT INTO rag_parents (parent_id,doc_title,section_path,doc_url,parent_text)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (parent_id) DO UPDATE SET
                   doc_title=EXCLUDED.doc_title,section_path=EXCLUDED.section_path,
                   doc_url=EXCLUDED.doc_url,parent_text=EXCLUDED.parent_text""",
                (
                    chunk.parent_id,
                    chunk.doc_title,
                    chunk.section_path,
                    chunk.doc_url,
                    chunk.parent_text,
                ),
            )
            cur.execute(
                """INSERT INTO rag_chunks (chunk_id,parent_id,content,acl_roles,status,embedding,fts_terms)
                   VALUES (%s,%s,%s,%s,%s,%s::vector,%s)
                   ON CONFLICT (chunk_id) DO UPDATE SET parent_id=EXCLUDED.parent_id,
                   content=EXCLUDED.content,acl_roles=EXCLUDED.acl_roles,status=EXCLUDED.status,
                   embedding=EXCLUDED.embedding,fts_terms=EXCLUDED.fts_terms""",
                (
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
        with self.store.transaction() as cur:
            cur.execute(
                f"""
                WITH q AS (SELECT to_tsquery('simple', %(words)s) AS ts)
                SELECT c.chunk_id,c.parent_id,p.doc_title,p.section_path,p.doc_url,
                       c.content,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM rag_chunks cx
                           WHERE cx.parent_id=c.parent_id
                             AND cx.status='active' AND NOT ({cx_acl_sql})
                       ) THEN '' ELSE p.parent_text END AS parent_text,
                       c.acl_roles,c.status,c.embedding::text,
                       ts_rank(c.fts_tsv, q.ts)::float8 AS sparse_score
                FROM rag_chunks c
                JOIN rag_parents p USING (parent_id), q
                WHERE {acl_sql}
                  AND c.fts_tsv @@ q.ts
                ORDER BY sparse_score DESC, c.chunk_id
                LIMIT %(k)s
                """,
                {**acl_params, **cx_acl_params, "words": " | ".join(words), "k": limit},
            )
            return [
                (RAGChunk(*row[:7], tuple(row[7]), row[8], tuple(json.loads(row[9])), row[10]), float(row[10]))
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
        with self.store.transaction() as cur:
            cur.execute(
                f"""
                SELECT c.chunk_id,c.parent_id,p.doc_title,p.section_path,p.doc_url,
                       c.content,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM rag_chunks cx
                           WHERE cx.parent_id=c.parent_id
                             AND cx.status='active' AND NOT ({cx_acl_sql})
                       ) THEN '' ELSE p.parent_text END AS parent_text,
                       c.acl_roles,c.status,c.embedding::text,
                       (1.0 - (c.embedding <=> %(vec)s::vector))::float8 AS dense_score
                FROM rag_chunks c
                JOIN rag_parents p USING (parent_id)
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
                (RAGChunk(*row[:7], tuple(row[7]), row[8], tuple(json.loads(row[9])), row[10]), float(row[10]))
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
        with self.store.transaction() as cur:
            cur.execute(
                f"""
                SELECT c.chunk_id,c.parent_id,p.doc_title,p.section_path,p.doc_url,
                       c.content,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM rag_chunks cx
                           WHERE cx.parent_id=c.parent_id
                             AND cx.status='active' AND NOT ({cx_acl_sql})
                       ) THEN '' ELSE p.parent_text END AS parent_text,
                       c.acl_roles,c.status,c.embedding::text
                FROM rag_chunks c
                JOIN rag_parents p USING (parent_id)
                WHERE {acl_sql}
                  AND c.parent_id = ANY(%(parent_ids)s)
                ORDER BY c.chunk_id
                """,
                {**acl_params, **cx_acl_params, "parent_ids": list(parent_ids)},
            )
            return [
                RAGChunk(*row[:7], tuple(row[7]), row[8], tuple(json.loads(row[9])), 0.0)
                for row in cur.fetchall()
            ]

    def index_document(self, scope: AuthScope, title: str, text: str, roles, *, parent=None, section=None, upload=False):
        pid = parent or uid("doc")
        key = f"knowledge/{pid}.txt" if upload else None
        if key:
            self.store.oss.put_text(key, text)
        with self.store.transaction() as cur:
            url = f"s3://{BUCKET}/{key}" if key else f"kb://{pid}"
            cur.execute(
                """INSERT INTO rag_parents(parent_id,doc_title,section_path,doc_url,parent_text,oss_key)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING parent_id""",
                (pid, title, section or title, url, "" if upload else text, key),
            )
            if not cur.fetchone():
                return {"parent_id": pid, "oss_key": key, "chunk_count": 0}
            chunks = []
            for i, start in enumerate(range(0, len(text), 700)):
                piece = text[start : start + 800]
                c = RAGChunk(
                    f"{pid}_{i + 1}",
                    pid,
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
                """INSERT INTO rag_chunks(chunk_id,parent_id,content,acl_roles,embedding,fts_terms)
                   VALUES (%s,%s,%s,%s,%s::vector,%s)""",
                chunks,
            )
        return {"parent_id": pid, "doc_title": title, "oss_key": key, "chunk_count": len(chunks)}

    def set_document_status(self, scope: AuthScope, parent_id: str, status: str) -> bool:
        if status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        with self.store.transaction() as cur:
            cur.execute(
                "UPDATE rag_chunks SET status=%s WHERE parent_id=%s RETURNING parent_id",
                (status, parent_id),
            )
            return bool(cur.fetchone())

    def list_documents_overview(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Summary rows for the reference Ops console (gracefully returns [] if RAG tables absent)."""
        if not self.store.rows("SELECT to_regclass('rag_parents') AS tbl")[0]["tbl"]:
            return []
        return self.store.rows(
            """SELECT p.parent_id AS chunk_id, p.parent_id, p.doc_title, p.section_path,
                      p.doc_url, p.oss_key,
                      COALESCE(
                          (SELECT array_agg(DISTINCT r ORDER BY r)
                           FROM rag_chunks c2, unnest(c2.acl_roles) AS r
                           WHERE c2.parent_id=p.parent_id),
                          ARRAY[]::text[]
                      ) AS acl_roles,
                      left(COALESCE(NULLIF(p.parent_text, ''), MIN(c.content), ''), 240) AS snippet,
                      count(c.chunk_id)::int AS chunk_count,
                      CASE WHEN bool_or(c.status = 'active') THEN 'active' ELSE 'disabled' END AS status
               FROM rag_parents p
               JOIN rag_chunks c USING (parent_id)
               GROUP BY p.parent_id, p.doc_title, p.section_path, p.doc_url, p.oss_key, p.parent_text
               ORDER BY (bool_or(c.status = 'active')) DESC, p.doc_title
               LIMIT %s""",
            (limit,),
        )

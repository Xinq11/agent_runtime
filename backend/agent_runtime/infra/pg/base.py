"""Core PostgreSQL transaction, span persistence, and schema bootstrap base."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
from typing import Any, List, Optional

from psycopg import sql
from psycopg.rows import dict_row

from agent_runtime.framework.core.contracts import SpanRecord


class PgBase:
    """Base PostgreSQL adapter with managed ConnectionPool, dict queries, spans, and bootstrap."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool: Optional[Any] = None

    def open_pool(self, *, min_size: int = 2, max_size: int = 16) -> None:
        if self._pool is not None:
            return
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(self.dsn, min_size=min_size, max_size=max_size, open=True)
        self._pool = pool

    def close_pool(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @contextmanager
    def transaction(self):
        if self._pool is not None:
            with self._pool.connection() as connection:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        yield cursor
            return

        import psycopg

        with psycopg.connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                yield cursor

    def rows(self, query: str, args=()):
        with self.transaction() as cur:
            cur.row_factory = dict_row
            cur.execute(query, args)
            return cur.fetchall()

    def save_span(self, span: SpanRecord) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO agent_spans
                   (trace_id,span_id,parent_span_id,service_name,operation_name,
                    start_ms,duration_ms,status,attributes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                   ON CONFLICT (trace_id,span_id) DO UPDATE SET
                   duration_ms=EXCLUDED.duration_ms, status=EXCLUDED.status,
                   attributes=EXCLUDED.attributes""",
                (
                    span.trace_id,
                    span.span_id,
                    span.parent_span_id,
                    span.service_name,
                    span.operation_name,
                    span.start_ms,
                    span.duration_ms,
                    span.status,
                    json.dumps(span.attributes),
                ),
            )

    def list_spans(self, trace_id: str) -> List[SpanRecord]:
        with self.transaction() as cursor:
            cursor.execute(
                """SELECT trace_id,span_id,parent_span_id,service_name,operation_name,
                          start_ms,duration_ms,status,attributes
                   FROM agent_spans WHERE trace_id=%s ORDER BY start_ms,span_id""",
                (trace_id,),
            )
            return [SpanRecord(*row) for row in cursor.fetchall()]

    def bootstrap(self) -> None:
        candidates = (
            Path(__file__).resolve().parents[4] / "deploy" / "sql",
            Path("/app/deploy/sql"),
            Path("/app/sql"),
        )
        sql_dir = next((p for p in candidates if p.exists()), candidates[0])
        with self.transaction() as cur:
            for fname in (
                "001_init_pg_schema.sql",
                "002_oss_and_kb.sql",
                "003_runs_artifacts_skills.sql",
                "004_message_feedback.sql",
                "005_workspace_attachments.sql",
                "006_drop_tenant_id.sql",
            ):
                mig = sql_dir / fname
                if mig.exists():
                    cur.execute(mig.read_text(), prepare=False)
            grafana_pw = os.getenv("GRAFANA_PG_PASSWORD", "").strip()
            if grafana_pw:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname='grafana_reader'")
                if cur.fetchone():
                    cur.execute(
                        sql.SQL("ALTER ROLE grafana_reader PASSWORD {}").format(
                            sql.Literal(grafana_pw)
                        )
                    )
        self.oss.head_bucket()

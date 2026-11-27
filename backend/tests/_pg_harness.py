"""Shared PostgreSQL + pgvector + MinIO test harness using an isolated `agent_runtime_test` DB."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import psycopg
from psycopg import conninfo, sql

from agent_runtime.framework.core.contracts import ExecutionContext, Message
from agent_runtime.infra.oss.minio import MinioObjectStore
from agent_runtime.infra.pg.postgres import DEFAULT_PG_DSN, PgOssStore


class EchoTestModel:
    """Test-only deterministic model double (never shipped in production runtime)."""

    def __init__(self, router=None) -> None:
        self.router = router

    async def generate(self, *, messages, tool_specs, ctx: ExecutionContext) -> Message:
        user_msgs = [m.content for m in messages if m.role == "user"]
        last_user = user_msgs[-1] if user_msgs else ""
        if self.router is not None:
            tool_msgs = [m for m in messages if m.role == "tool"]
            tool_names = {getattr(t, "name", "") for t in tool_specs}
            return self.router(ctx, last_user, tool_msgs, tool_names)
        return Message(role="assistant", content=f"已收到消息：{last_user}")

TEST_DB_NAME = os.getenv("AGENT_TEST_DB", "agent_runtime_test")
TEST_BUCKET = os.getenv("AGENT_TEST_BUCKET", "agent-artifacts-test")
_PROTECTED_DBS = frozenset({"agent_runtime", "postgres", "template0", "template1"})


def validate_test_db_name(db_name: str) -> str:
    """Safety guard: refuse destructive test resets against production or system databases."""
    cleaned = (db_name or "").strip()
    low = cleaned.lower()
    if not cleaned or low in _PROTECTED_DBS or not (low.endswith("_test") or low.startswith("test_")):
        raise RuntimeError(
            f"Refusing to run destructive test harness against non-test DB '{db_name}'; "
            "AGENT_TEST_DB must end with '_test' or start with 'test_'."
        )
    return cleaned

_SHARED_STORE: Optional[PgOssStore] = None
_ORIGINAL_PG_DBNAME: Optional[str] = None


def _sql_dir() -> Path:
    candidates = (
        Path(__file__).resolve().parents[2] / "deploy" / "sql",
        Path("/app/deploy/sql"),
        Path("/app/sql"),
    )
    return next((p for p in candidates if p.exists()), candidates[0])


def get_test_store() -> PgOssStore:
    """Create/migrate isolated `agent_runtime_test` DB & bucket once, returning a pooled `PgOssStore`."""
    global _SHARED_STORE, _ORIGINAL_PG_DBNAME
    if _SHARED_STORE is not None:
        return _SHARED_STORE

    db_name = validate_test_db_name(TEST_DB_NAME)
    base_dsn = os.getenv("PG_DSN", DEFAULT_PG_DSN)
    orig_info = conninfo.conninfo_to_dict(base_dsn)
    _ORIGINAL_PG_DBNAME = str(orig_info.get("dbname") or "")
    if _ORIGINAL_PG_DBNAME and _ORIGINAL_PG_DBNAME == db_name and _ORIGINAL_PG_DBNAME in _PROTECTED_DBS:
        raise RuntimeError(f"Refusing to target protected DB '{db_name}'")
    admin_dsn = conninfo.make_conninfo(base_dsn, dbname="postgres")
    test_dsn = conninfo.make_conninfo(base_dsn, dbname=db_name)

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db_name,))
            if not cur.fetchone():
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))

    os.environ["PG_DSN"] = test_dsn
    os.environ.setdefault("GRAFANA_PG_PASSWORD", "traces_demo")

    oss = MinioObjectStore(bucket=TEST_BUCKET)
    oss.ensure_bucket()

    store = PgOssStore(test_dsn, oss=oss)
    store.open_pool(min_size=1, max_size=8)

    sdir = _sql_dir()
    with store.transaction() as cur:
        cur.execute("SELECT to_regclass('public.agent_threads')")
        if not cur.fetchone()[0]:
            cur.execute((sdir / "001_init_pg_schema.sql").read_text(), prepare=False)
    store.bootstrap()

    _SHARED_STORE = store
    return store


def reset_test_db() -> PgOssStore:
    """Truncate all domain tables in `agent_runtime_test` so every test starts from a clean slate."""
    db_name = validate_test_db_name(TEST_DB_NAME)
    store = get_test_store()
    with store.transaction() as cur:
        cur.execute("SELECT current_database()")
        actual_db = str(cur.fetchone()[0])
        if actual_db != db_name or actual_db in _PROTECTED_DBS:
            raise RuntimeError(f"Refusing TRUNCATE on unexpected database '{actual_db}' (expected '{db_name}')")
        cur.execute(
            """TRUNCATE agent_threads, session_messages, session_summaries, message_feedback,
                        tool_approvals, tool_outputs, tool_runs, rag_parents, rag_chunks,
                        agent_spans, runtime_traces, agent_runs, workspace_files,
                        agent_skills, agent_skill_versions
               RESTART IDENTITY CASCADE"""
        )
    return store

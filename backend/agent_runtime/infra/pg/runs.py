"""PostgreSQL repository mixin for durable `agent_runs` lifecycle and Abort transitions."""

from __future__ import annotations

from typing import Optional, Tuple

from agent_runtime.framework.core.contracts import RunRecord


class PgRunsMixin:
    """Short-transaction durable run state machine replacing long-lived advisory locks."""

    _RUN_COLS = """run_id,thread_id,actor_id,trace_id,request_id,status,owner_epoch,
                   extract(epoch from cancel_requested_at)::float8,error_detail"""

    def create_run(self, rec: RunRecord) -> Tuple[bool, Optional[str]]:
        import psycopg

        try:
            with self.transaction() as cur:
                cur.execute(
                    """INSERT INTO agent_runs
                       (run_id,thread_id,actor_id,trace_id,request_id,status,owner_epoch)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        rec.run_id,
                        rec.thread_id,
                        rec.actor_id,
                        rec.trace_id,
                        rec.request_id,
                        rec.status,
                        rec.owner_epoch,
                    ),
                )
            return True, None
        except psycopg.errors.UniqueViolation:
            return False, "当前对话已有正在执行或待审批的任务，请等待完成或先点击停止"

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        with self.transaction() as cur:
            cur.execute(f"SELECT {self._RUN_COLS} FROM agent_runs WHERE run_id=%s", (run_id,))
            row = cur.fetchone()
            return RunRecord(*row) if row else None

    def get_active_run(self, thread_id: str) -> Optional[RunRecord]:
        with self.transaction() as cur:
            cur.execute(
                f"SELECT {self._RUN_COLS} FROM agent_runs WHERE thread_id=%s "
                "AND status IN ('running','paused') ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            )
            row = cur.fetchone()
            return RunRecord(*row) if row else None

    def mark_run_cancelled(self, run_id: str) -> Optional[RunRecord]:
        with self.transaction() as cur:
            cur.execute(
                f"""UPDATE agent_runs
                    SET status='cancelled', cancel_requested_at=COALESCE(cancel_requested_at, now()),
                        owner_epoch=owner_epoch+1, updated_at=now()
                    WHERE run_id=%s AND status IN ('running','paused')
                    RETURNING {self._RUN_COLS}""",
                (run_id,),
            )
            row = cur.fetchone()
            if row:
                return RunRecord(*row)
            cur.execute(f"SELECT {self._RUN_COLS} FROM agent_runs WHERE run_id=%s", (run_id,))
            row = cur.fetchone()
            return RunRecord(*row) if row else None

    def transition_run_status(
        self,
        run_id: str,
        *,
        expected: Tuple[str, ...],
        new_status: str,
        error_detail: Optional[str] = None,
    ) -> Optional[RunRecord]:
        with self.transaction() as cur:
            cur.execute(
                f"""UPDATE agent_runs
                    SET status=%s, owner_epoch=owner_epoch+1,
                        error_detail=COALESCE(%s, error_detail), updated_at=now()
                    WHERE run_id=%s AND status = ANY(%s)
                    RETURNING {self._RUN_COLS}""",
                (new_status, error_detail, run_id, list(expected)),
            )
            row = cur.fetchone()
            return RunRecord(*row) if row else None

    def heartbeat_run(self, run_id: str) -> bool:
        """Refresh `updated_at` lease timestamp for an actively executing run."""
        with self.transaction() as cur:
            cur.execute(
                """UPDATE agent_runs
                   SET updated_at=clock_timestamp()
                   WHERE run_id=%s AND status='running'
                   RETURNING run_id""",
                (run_id,),
            )
            return cur.fetchone() is not None

    def reap_stale_runs(
        self,
        stale_after_sec: float = 120.0,
        *,
        thread_id: Optional[str] = None,
        error_detail: str = "stale_run_recovered",
    ) -> int:
        """Mark zombie `running` runs whose lease expired as `failed` (never touches `paused`)."""
        threshold = max(0.0, float(stale_after_sec))
        with self.transaction() as cur:
            if thread_id:
                cur.execute(
                    """UPDATE agent_runs
                       SET status='failed',
                           owner_epoch=owner_epoch+1,
                           error_detail=COALESCE(error_detail, %s),
                           updated_at=clock_timestamp()
                       WHERE status='running'
                         AND thread_id=%s
                         AND updated_at < clock_timestamp() - (%s * interval '1 second')
                       RETURNING run_id""",
                    (error_detail, thread_id, threshold),
                )
            else:
                cur.execute(
                    """UPDATE agent_runs
                       SET status='failed',
                           owner_epoch=owner_epoch+1,
                           error_detail=COALESCE(error_detail, %s),
                           updated_at=clock_timestamp()
                       WHERE status='running'
                         AND updated_at < clock_timestamp() - (%s * interval '1 second')
                       RETURNING run_id""",
                    (error_detail, threshold),
                )
            return len(cur.fetchall())

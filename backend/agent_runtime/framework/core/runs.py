"""Durable Run lifecycle & safe Abort state machine (`RunService`).

Addresses the concurrency trap where holding a PG advisory lock across a long LLM
stream blocks abort requests:
  - Starting a run registers a row in `agent_runs` in a short transaction (no long lock).
  - Aborting updates `agent_runs` (`status='cancelled'`, `cancel_requested_at=now()`)
    and signals the live asyncio.Task / cancellation event immediately.
  - Resuming an approval checks `agent_runs.status == 'paused'` so a cancelled run
    can NEVER be resurrected by a stale approval decision.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from agent_runtime.framework.core.config import uid
from agent_runtime.framework.core.contracts import AuthScope, RunRecord


class RunConflictError(RuntimeError):
    """Raised when a thread already has an active running/paused run."""


class RunNotFoundError(LookupError):
    """Raised when a run_id does not exist or does not belong to the caller."""


class RunStateError(RuntimeError):
    """Raised when attempting an invalid state transition (e.g. resuming a cancelled run)."""


class RunService:
    """Manages durable `agent_runs` state transitions and live in-process cancellation."""

    def __init__(self, store: Any, *, db_poll_interval_sec: float = 0.5) -> None:
        self.store = store
        self._db_poll_interval_sec = max(0.0, db_poll_interval_sec)
        self._tasks: Dict[str, asyncio.Task] = {}
        self._cancel_flags: Dict[str, asyncio.Event] = {}
        self._last_db_check: Dict[str, float] = {}

    def start_run(
        self,
        *,
        scope: AuthScope,
        thread_id: str,
        trace_id: str,
        request_id: str,
        run_id: Optional[str] = None,
    ) -> RunRecord:
        rid = run_id or uid("run")
        rec = RunRecord(
            run_id=rid,
            tenant_id=scope.tenant_id,
            thread_id=thread_id,
            actor_id=scope.user_id,
            trace_id=trace_id,
            request_id=request_id,
            status="running",
            owner_epoch=1,
        )
        ok, err = self.store.create_run(rec)
        if not ok:
            raise RunConflictError(err or "当前对话已有正在执行或待审批的任务")
        self._cancel_flags[rid] = asyncio.Event()
        self._last_db_check[rid] = time.monotonic()
        return rec

    def attach_task(self, run_id: str, task: asyncio.Task) -> None:
        self._tasks[run_id] = task
        self._cancel_flags.setdefault(run_id, asyncio.Event())

    def detach_task(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._cancel_flags.pop(run_id, None)
        self._last_db_check.pop(run_id, None)

    def is_cancelled(self, run_id: str) -> bool:
        ev = self._cancel_flags.get(run_id)
        if ev is not None:
            if ev.is_set():
                return True
            now = time.monotonic()
            last = self._last_db_check.get(run_id, 0.0)
            if now - last < self._db_poll_interval_sec:
                return False
            self._last_db_check[run_id] = now
        rec = self.store.get_run(run_id)
        cancelled = bool(rec and (rec.status == "cancelled" or rec.cancel_requested_at is not None))
        if cancelled and ev is not None:
            ev.set()
        return cancelled

    def get_run(self, scope: AuthScope, run_id: str) -> RunRecord:
        rec = self.store.get_run(run_id)
        if not rec or rec.tenant_id != scope.tenant_id or rec.actor_id != scope.user_id:
            raise RunNotFoundError("运行记录不存在")
        return rec

    def get_active_run_for_thread(self, scope: AuthScope, thread_id: str) -> Optional[RunRecord]:
        rec = self.store.get_active_run(thread_id)
        if not rec or rec.tenant_id != scope.tenant_id or rec.actor_id != scope.user_id:
            return None
        return rec

    def request_abort(
        self,
        *,
        scope: AuthScope,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> RunRecord:
        """Idempotently cancel an active or paused run by `run_id` or `thread_id`."""
        rec: Optional[RunRecord] = None
        if run_id:
            rec = self.get_run(scope, run_id)
        elif thread_id:
            rec = self.get_active_run_for_thread(scope, thread_id)
            if rec is None:
                raise RunNotFoundError("当前对话没有正在执行或暂停的任务")
        else:
            raise RunNotFoundError("请提供 run_id 或 thread_id")

        if rec.status in ("completed", "cancelled", "failed"):
            return rec

        updated = self.store.mark_run_cancelled(rec.run_id, scope.tenant_id)
        ev = self._cancel_flags.get(rec.run_id)
        if ev is not None:
            ev.set()
        task = self._tasks.get(rec.run_id)
        if task is not None and not task.done():
            task.cancel()
        return updated or rec

    def mark_paused(self, run_id: str, tenant_id: str) -> Optional[RunRecord]:
        return self.store.transition_run_status(
            run_id, tenant_id, expected=("running",), new_status="paused"
        )

    def mark_resumed(self, run_id: str, tenant_id: str) -> RunRecord:
        updated = self.store.transition_run_status(
            run_id, tenant_id, expected=("paused",), new_status="running"
        )
        if updated is None:
            raise RunStateError("该运行已取消或不再处于暂停状态，无法恢复")
        self._cancel_flags[run_id] = asyncio.Event()
        self._last_db_check[run_id] = time.monotonic()
        return updated

    def mark_finished(
        self,
        run_id: str,
        tenant_id: str,
        *,
        status: str,
        error_detail: Optional[str] = None,
    ) -> Optional[RunRecord]:
        return self.store.transition_run_status(
            run_id,
            tenant_id,
            expected=("running", "paused"),
            new_status=status,
            error_detail=error_detail,
        )

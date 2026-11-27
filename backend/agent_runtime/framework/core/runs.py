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
from contextlib import suppress
import os
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
    """Manages durable `agent_runs` state transitions, lease heartbeats, and live cancellation."""

    def __init__(
        self,
        store: Any,
        *,
        db_poll_interval_sec: float = 0.5,
        stale_run_timeout_sec: Optional[float] = None,
        heartbeat_interval_sec: Optional[float] = None,
        obs: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.obs = obs
        self._db_poll_interval_sec = max(0.0, db_poll_interval_sec)
        self._stale_run_timeout_sec = (
            max(1.0, float(stale_run_timeout_sec))
            if stale_run_timeout_sec is not None
            else max(1.0, float(os.getenv("AGENT_STALE_RUN_TIMEOUT_SEC", "120")))
        )
        self._heartbeat_interval_sec = (
            max(0.2, float(heartbeat_interval_sec))
            if heartbeat_interval_sec is not None
            else min(15.0, max(1.0, self._stale_run_timeout_sec / 3.0))
        )
        self._tasks: Dict[str, asyncio.Task] = {}
        self._heartbeats: Dict[str, asyncio.Task] = {}
        self._cancel_flags: Dict[str, asyncio.Event] = {}
        self._last_db_check: Dict[str, float] = {}

    def reap_stale_runs(
        self,
        *,
        stale_after_sec: Optional[float] = None,
        thread_id: Optional[str] = None,
        reason: str = "lease_expired",
    ) -> int:
        """Recover crashed zombie `running` runs whose lease expired (never reaps `paused`)."""
        reaper = getattr(self.store, "reap_stale_runs", None)
        if reaper is None:
            return 0
        threshold = self._stale_run_timeout_sec if stale_after_sec is None else max(0.0, float(stale_after_sec))
        reaped = int(reaper(stale_after_sec=threshold, thread_id=thread_id) or 0)
        if reaped > 0 and self.obs is not None:
            counter = getattr(self.obs, "runs_recovered", None)
            if counter is not None:
                counter.labels(reason).inc(reaped)
            log_fn = getattr(self.obs, "log_event", None)
            if log_fn is not None:
                log_fn(
                    "stale_runs_recovered",
                    "system",
                    thread_id or "system",
                    count=reaped,
                    reason=reason,
                )
        return reaped

    def heartbeat_run(self, run_id: str) -> bool:
        hb = getattr(self.store, "heartbeat_run", None)
        if hb is None:
            return False
        return bool(hb(run_id))

    async def _heartbeat_loop(self, run_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval_sec)
                if run_id not in self._tasks:
                    break
                with suppress(Exception):
                    still_running = await asyncio.to_thread(self.heartbeat_run, run_id)
                    if not still_running:
                        break
        except asyncio.CancelledError:
            return

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
            thread_id=thread_id,
            actor_id=scope.user_id,
            trace_id=trace_id,
            request_id=request_id,
            status="running",
            owner_epoch=1,
        )
        ok, err = self.store.create_run(rec)
        if not ok:
            # Auto-recover crashed zombie `running` run on the same thread before failing with 409
            if self.reap_stale_runs(thread_id=thread_id, reason="conflict_recovery") > 0:
                ok, err = self.store.create_run(rec)
        if not ok:
            raise RunConflictError(err or "当前对话已有正在执行或待审批的任务")
        self._cancel_flags[rid] = asyncio.Event()
        self._last_db_check[rid] = time.monotonic()
        return rec

    def attach_task(self, run_id: str, task: asyncio.Task) -> None:
        self._tasks[run_id] = task
        self._cancel_flags.setdefault(run_id, asyncio.Event())
        if hasattr(self.store, "heartbeat_run") and run_id not in self._heartbeats:
            with suppress(RuntimeError):
                self._heartbeats[run_id] = asyncio.create_task(self._heartbeat_loop(run_id))

    def detach_task(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        hb = self._heartbeats.pop(run_id, None)
        if hb is not None and not hb.done():
            hb.cancel()
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
        if not rec or rec.actor_id != scope.user_id:
            raise RunNotFoundError("运行记录不存在")
        return rec

    def get_run_by_id(self, run_id: str) -> RunRecord:
        """Internal run lookup used only after explicit `approval:decide` authorization."""
        rec = self.store.get_run(run_id)
        if not rec:
            raise RunNotFoundError("运行记录不存在")
        return rec

    def get_active_run_for_thread(self, scope: AuthScope, thread_id: str) -> Optional[RunRecord]:
        rec = self.store.get_active_run(thread_id)
        if not rec or rec.actor_id != scope.user_id:
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

        updated = self.store.mark_run_cancelled(rec.run_id)
        ev = self._cancel_flags.get(rec.run_id)
        if ev is not None:
            ev.set()
        task = self._tasks.get(rec.run_id)
        if task is not None and not task.done():
            task.cancel()
        return updated or rec

    def mark_paused(self, run_id: str) -> Optional[RunRecord]:
        return self.store.transition_run_status(run_id, expected=("running",), new_status="paused")

    def mark_resumed(self, run_id: str) -> RunRecord:
        updated = self.store.transition_run_status(run_id, expected=("paused",), new_status="running")
        if updated is None:
            raise RunStateError("该运行已取消或不再处于暂停状态，无法恢复")
        self._cancel_flags[run_id] = asyncio.Event()
        self._last_db_check[run_id] = time.monotonic()
        return updated

    def mark_finished(
        self,
        run_id: str,
        *,
        status: str,
        error_detail: Optional[str] = None,
    ) -> Optional[RunRecord]:
        return self.store.transition_run_status(
            run_id,
            expected=("running", "paused"),
            new_status=status,
            error_detail=error_detail,
        )

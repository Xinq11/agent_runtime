"""In-memory durable run, approval ticket, and atomic idempotent tool-run mixin."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import time
from typing import Optional, Tuple
from uuid import uuid4

from agent_runtime.framework.core.approval_rules import make_tool_run_record, validate_approval_ticket
from agent_runtime.framework.core.contracts import (
    ApprovalDecision,
    ApprovalTicket,
    RunRecord,
    ToolRunRecord,
)


class MemoryApprovalsMixin:
    """Runs, approvals, and atomic claim/consume operations sharing `self._lock`."""

    # --- Durable Runs & Abort ---
    def create_run(self, rec: RunRecord) -> Tuple[bool, Optional[str]]:
        with self._lock:
            for existing in self._runs.values():
                if existing.thread_id == rec.thread_id and existing.status in ("running", "paused"):
                    return False, "当前对话已有正在执行或待审批的任务"
            self._runs[rec.run_id] = deepcopy(rec)
            return True, None

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        with self._lock:
            rec = self._runs.get(run_id)
            return deepcopy(rec) if rec else None

    def get_active_run(self, thread_id: str) -> Optional[RunRecord]:
        with self._lock:
            for rec in self._runs.values():
                if rec.thread_id == thread_id and rec.status in ("running", "paused"):
                    return deepcopy(rec)
            return None

    def mark_run_cancelled(self, run_id: str, tenant_id: str) -> Optional[RunRecord]:
        with self._lock:
            rec = self._runs.get(run_id)
            if not rec or rec.tenant_id != tenant_id:
                return None
            if rec.status in ("running", "paused"):
                rec.status = "cancelled"
                rec.cancel_requested_at = time.time()
                rec.owner_epoch += 1
            return deepcopy(rec)

    def transition_run_status(
        self,
        run_id: str,
        tenant_id: str,
        *,
        expected: Tuple[str, ...],
        new_status: str,
        error_detail: Optional[str] = None,
    ) -> Optional[RunRecord]:
        with self._lock:
            rec = self._runs.get(run_id)
            if not rec or rec.tenant_id != tenant_id or rec.status not in expected:
                return None
            rec.status = new_status
            rec.owner_epoch += 1
            if error_detail is not None:
                rec.error_detail = error_detail
            return deepcopy(rec)

    # --- Approvals & Atomic Tool Run Claim ---
    def get_or_create_approval(
        self, ctx, op_key, tool_name, args_hash, summary, ttl_sec=600
    ) -> ApprovalTicket:
        now = time.time()
        with self._lock:
            for ticket in self._approvals.values():
                if (
                    ticket.tenant_id == ctx.auth_scope.tenant_id
                    and ticket.thread_id == ctx.thread_id
                    and ticket.actor_id == ctx.auth_scope.user_id
                    and ticket.op_key == op_key
                    and ticket.tool_name == tool_name
                    and ticket.args_hash == args_hash
                    and ticket.policy_rev == ctx.policy_rev
                    and (ticket.expires_at > now or ticket.consumed_at is not None)
                ):
                    return deepcopy(ticket)
            ticket = ApprovalTicket(
                review_id=uuid4().hex,
                tenant_id=ctx.auth_scope.tenant_id,
                thread_id=ctx.thread_id,
                actor_id=ctx.auth_scope.user_id,
                op_key=op_key,
                tool_name=tool_name,
                args_hash=args_hash,
                policy_rev=ctx.policy_rev,
                summary=deepcopy(summary),
                expires_at=now + ttl_sec,
            )
            self._approvals[ticket.review_id] = ticket
            return deepcopy(ticket)

    def find_approval_thread(self, review_id: str, scope) -> Optional[str]:
        with self._lock:
            ticket = self._approvals.get(review_id)
            if not ticket or ticket.tenant_id != scope.tenant_id or ticket.actor_id != scope.user_id:
                return None
            return ticket.thread_id

    def record_approval_decision(self, decision: ApprovalDecision) -> ApprovalTicket:
        if not decision.approver_id or type(decision.approved) is not bool:
            raise ValueError("invalid_approval_decision")
        value = "approved" if decision.approved else "rejected"
        with self._lock:
            ticket = self._approvals.get(decision.review_id)
            if not ticket:
                raise ValueError("invalid_or_expired_decision")
            if ticket.decision == "pending" and ticket.expires_at > time.time():
                ticket.decision = value
                ticket.approver_id = decision.approver_id
                return deepcopy(ticket)
            if ticket.decision == value and ticket.approver_id == decision.approver_id:
                return deepcopy(ticket)
            raise ValueError("invalid_or_expired_decision")

    def authorize_and_claim_run(
        self, ctx, op_key, tool_name, args_hash, review_id=None
    ) -> Tuple[bool, ToolRunRecord, Optional[str]]:
        run = make_tool_run_record(ctx, op_key, tool_name, args_hash)
        key = (run.tenant_id, op_key)
        with self._lock:
            old = self._tool_runs.get(key)
            if old:
                if (old.actor_id, old.tool_name, old.args_hash) != (run.actor_id, tool_name, args_hash):
                    return False, replace(run, status="failed"), "op_key_conflict"
                return False, deepcopy(old), None if old.status == "done" else old.error_code or old.status
            if review_id is None:
                if any(t.tenant_id == run.tenant_id and t.op_key == op_key for t in self._approvals.values()):
                    return False, replace(run, status="failed"), "approval_mismatch"
            else:
                ticket = self._approvals.get(review_id)
                err = validate_approval_ticket(ticket, ctx, run)
                if err:
                    return False, replace(run, status="failed"), err
                ticket.consumed_at = time.time()
            self._tool_runs[key] = run
            return True, deepcopy(run), None

    def finish_tool_run(
        self, tenant_id, op_key, *, status, receipt=None, preview=None, result_ref=None, error_code=None
    ) -> ToolRunRecord:
        if status not in ("done", "failed", "reconcile"):
            raise ValueError("invalid run status")
        with self._lock:
            rec = self._tool_runs[(tenant_id, op_key)]
            if rec.status in ("running", "reconcile"):
                rec.status = status
                rec.receipt = deepcopy(receipt)
                rec.preview = preview
                rec.result_ref = result_ref
                rec.error_code = error_code
            return deepcopy(rec)

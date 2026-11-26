"""PostgreSQL repository mixin for high-risk approvals and atomic idempotent tool runs."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Optional, Tuple
from uuid import uuid4

from agent_runtime.framework.core.approval_rules import make_tool_run_record, validate_approval_ticket
from agent_runtime.framework.core.contracts import (
    ApprovalDecision,
    ApprovalTicket,
    AuthScope,
    ToolRunRecord,
)


class PgApprovalsMixin:
    """Transactional approval ticket consumption and idempotent tool-run claiming."""

    _TICKET = """review_id,tenant_id,thread_id,actor_id,op_key,tool_name,args_hash,policy_rev,summary,
                 extract(epoch from expires_at)::float8,decision,approver_id,
                 extract(epoch from consumed_at)::float8"""
    _TOOL_RUN = """tenant_id,op_key,actor_id,tool_name,args_hash,status,receipt,result_ref,preview,
                   error_code,extract(epoch from lease_until)::float8"""

    @staticmethod
    def _lock_op(cur, tenant: str, key: str) -> None:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (json.dumps([tenant, key]),),
        )

    def get_or_create_approval(
        self, ctx, op_key, tool_name, args_hash, summary, ttl_sec=600
    ) -> ApprovalTicket:
        tenant, actor = ctx.auth_scope.tenant_id, ctx.auth_scope.user_id
        fields = (tenant, ctx.thread_id, actor, op_key, tool_name, args_hash, ctx.policy_rev)
        with self.transaction() as cur:
            self._lock_op(cur, tenant, op_key)
            cur.execute(
                f"""SELECT {self._TICKET} FROM tool_approvals
                    WHERE (tenant_id,thread_id,actor_id,op_key,tool_name,args_hash,policy_rev)
                        = (%s,%s,%s,%s,%s,%s,%s)
                      AND (expires_at>clock_timestamp() OR consumed_at IS NOT NULL)
                    ORDER BY expires_at DESC LIMIT 1""",
                fields,
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"""INSERT INTO tool_approvals
                        (review_id,tenant_id,thread_id,actor_id,op_key,tool_name,args_hash,
                         policy_rev,summary,expires_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                                clock_timestamp()+%s*interval '1 second') RETURNING {self._TICKET}""",
                    (uuid4().hex, *fields, json.dumps(summary), ttl_sec),
                )
                row = cur.fetchone()
            return ApprovalTicket(*row)

    def find_approval_thread(self, review_id: str, scope: AuthScope) -> Optional[str]:
        rows = self.rows(
            "SELECT thread_id FROM tool_approvals WHERE review_id=%s AND tenant_id=%s AND actor_id=%s",
            (review_id, scope.tenant_id, scope.user_id),
        )
        return rows[0]["thread_id"] if rows else None

    def record_approval_decision(self, decision: ApprovalDecision) -> ApprovalTicket:
        if not decision.approver_id or type(decision.approved) is not bool:
            raise ValueError("invalid_approval_decision")
        value = "approved" if decision.approved else "rejected"
        with self.transaction() as cur:
            cur.execute(
                f"""UPDATE tool_approvals SET decision=%s,approver_id=%s
                    WHERE review_id=%s AND ((decision='pending' AND expires_at>clock_timestamp())
                    OR (decision=%s AND approver_id=%s)) RETURNING {self._TICKET}""",
                (value, decision.approver_id, decision.review_id, value, decision.approver_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("invalid_or_expired_decision")
            return ApprovalTicket(*row)

    def authorize_and_claim_run(
        self, ctx, op_key, tool_name, args_hash, review_id=None
    ) -> Tuple[bool, ToolRunRecord, Optional[str]]:
        run = make_tool_run_record(ctx, op_key, tool_name, args_hash)
        key = (run.tenant_id, op_key)
        with self.transaction() as cur:
            self._lock_op(cur, *key)
            cur.execute(f"SELECT {self._TOOL_RUN} FROM tool_runs WHERE tenant_id=%s AND op_key=%s", key)
            row = cur.fetchone()
            if row:
                old = ToolRunRecord(*row)
                if (old.actor_id, old.tool_name, old.args_hash) != (run.actor_id, tool_name, args_hash):
                    return False, replace(run, status="failed"), "op_key_conflict"
                return False, old, None if old.status == "done" else old.error_code or old.status
            if review_id is None:
                cur.execute("SELECT 1 FROM tool_approvals WHERE tenant_id=%s AND op_key=%s LIMIT 1", key)
                if cur.fetchone():
                    return False, replace(run, status="failed"), "approval_mismatch"
            else:
                cur.execute(
                    f"SELECT {self._TICKET} FROM tool_approvals WHERE review_id=%s FOR UPDATE",
                    (review_id,),
                )
                row = cur.fetchone()
                error = validate_approval_ticket(ApprovalTicket(*row) if row else None, ctx, run)
                if error:
                    return False, replace(run, status="failed"), error
                cur.execute(
                    """UPDATE tool_approvals SET consumed_at=clock_timestamp()
                       WHERE review_id=%s AND expires_at>clock_timestamp() RETURNING review_id""",
                    (review_id,),
                )
                if not cur.fetchone():
                    return False, replace(run, status="failed"), "approval_expired"
            cur.execute(
                """INSERT INTO tool_runs (tenant_id,op_key,actor_id,tool_name,args_hash,status,lease_until)
                   VALUES (%s,%s,%s,%s,%s,'running',to_timestamp(%s))""",
                (*key, run.actor_id, tool_name, args_hash, run.lease_until),
            )
            return True, run, None

    def finish_tool_run(
        self, tenant_id, op_key, *, status, receipt=None, preview=None, result_ref=None, error_code=None
    ) -> ToolRunRecord:
        if status not in ("done", "failed", "reconcile"):
            raise ValueError("invalid run status")
        with self.transaction() as cur:
            cur.execute(
                f"""UPDATE tool_runs SET status=%s,receipt=%s::jsonb,preview=%s,result_ref=%s,error_code=%s
                    WHERE tenant_id=%s AND op_key=%s AND status IN ('running','reconcile')
                    RETURNING {self._TOOL_RUN}""",
                (status, json.dumps(receipt), preview, result_ref, error_code, tenant_id, op_key),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"SELECT {self._TOOL_RUN} FROM tool_runs WHERE tenant_id=%s AND op_key=%s",
                    (tenant_id, op_key),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(op_key)
            return ToolRunRecord(*row)

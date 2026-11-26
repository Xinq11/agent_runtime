"""Shared domain validation rules for approval tickets and idempotent tool runs."""

from __future__ import annotations

import time
from typing import Optional

from agent_runtime.framework.core.contracts import ApprovalTicket, ExecutionContext, ToolRunRecord


def make_tool_run_record(
    ctx: ExecutionContext, op_key: str, tool_name: str, args_hash: str
) -> ToolRunRecord:
    return ToolRunRecord(
        ctx.auth_scope.tenant_id,
        op_key,
        ctx.auth_scope.user_id,
        tool_name,
        args_hash,
        "running",
        lease_until=time.time() + 60,
    )


def validate_approval_ticket(
    ticket: Optional[ApprovalTicket],
    ctx: ExecutionContext,
    run: ToolRunRecord,
) -> Optional[str]:
    if not ticket or (
        ticket.tenant_id,
        ticket.actor_id,
        ticket.thread_id,
        ticket.op_key,
        ticket.tool_name,
        ticket.args_hash,
        ticket.policy_rev,
    ) != (
        run.tenant_id,
        run.actor_id,
        ctx.thread_id,
        run.op_key,
        run.tool_name,
        run.args_hash,
        ctx.policy_rev,
    ):
        return "approval_mismatch"
    if ticket.consumed_at is not None:
        return "approval_consumed"
    if ticket.expires_at <= time.time():
        return "approval_expired"
    if ticket.decision != "approved":
        return "approval_" + ticket.decision
    return None

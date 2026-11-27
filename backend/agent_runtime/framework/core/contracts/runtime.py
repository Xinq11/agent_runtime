"""Durable run, approval ticket, idempotent tool run, and trace span records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ApprovalTicket:
    review_id: str
    thread_id: str
    actor_id: str
    op_key: str
    tool_name: str
    args_hash: str
    policy_rev: int
    summary: Dict[str, Any]
    expires_at: float
    decision: str = "pending"  # pending | approved | rejected
    approver_id: Optional[str] = None
    consumed_at: Optional[float] = None
    run_id: str = ""


@dataclass
class ApprovalDecision:
    review_id: str
    approved: bool
    approver_id: str
    reason: str = ""


@dataclass
class ReconcileResult:
    """Outcome returned by a write tool's optional `reconcile(op_key, args, ctx)` callback."""

    status: str  # done | failed | pending
    content: str = ""
    receipt: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in ("done", "failed", "pending"):
            raise ValueError(f"Invalid ReconcileResult status: {self.status!r}")


@dataclass
class ToolRunRecord:
    op_key: str
    actor_id: str
    tool_name: str
    args_hash: str
    status: str  # running | done | failed | reconcile
    receipt: Optional[Dict[str, Any]] = None
    result_ref: Optional[str] = None
    preview: Optional[str] = None
    error_code: Optional[str] = None
    lease_until: float = 0.0


@dataclass
class RunRecord:
    """Durable conversation turn run record backing concurrency control & safe Abort."""

    run_id: str
    thread_id: str
    actor_id: str
    trace_id: str
    request_id: str
    status: str = "running"  # running | paused | completed | cancelled | failed
    owner_epoch: int = 1
    cancel_requested_at: Optional[float] = None
    error_detail: Optional[str] = None


@dataclass
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    service_name: str
    operation_name: str
    start_ms: int
    duration_ms: int
    status: str = "ok"
    attributes: Dict[str, Any] = field(default_factory=dict)

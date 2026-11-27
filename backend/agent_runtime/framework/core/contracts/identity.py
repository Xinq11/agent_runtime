"""Identity, execution context, risk classification, and SSE v1 envelope contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

PROTOCOL_VERSION = "1.0"


class RiskLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    HIGH_RISK = "high_risk"


@dataclass(frozen=True)
class AuthScope:
    """Caller identity & tool scope carried across execution boundaries.

    Each customer deploys independently; the kernel only requires a trusted `user_id`
    (plus optional business `roles` and custom `attrs`).
    `allowed_tools=None` means unrestricted; `allowed_tools=()` means zero tools allowed.
    """

    user_id: str
    roles: Tuple[str, ...] = ()
    allowed_tools: Optional[Tuple[str, ...]] = None
    allow_write: bool = True
    authenticated: bool = False
    attrs: Tuple[Tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("AuthScope.user_id must be a non-empty string")
        object.__setattr__(self, "roles", tuple(self.roles or ()))
        if self.allowed_tools is not None:
            object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        if isinstance(self.attrs, dict):
            object.__setattr__(self, "attrs", tuple(sorted((str(k), v) for k, v in self.attrs.items())))
        else:
            object.__setattr__(self, "attrs", tuple((str(k), v) for k, v in (self.attrs or ())))

    def narrowed(
        self,
        *,
        allowed_tools: Tuple[str, ...],
        allow_write: bool = False,
    ) -> "AuthScope":
        """Monotonic permission narrowing: sub-agents can only shrink scope, never expand."""
        if self.allowed_tools is not None:
            intersected = tuple(sorted(set(self.allowed_tools) & set(allowed_tools)))
        else:
            intersected = tuple(sorted(set(allowed_tools)))
        return AuthScope(
            user_id=self.user_id,
            roles=self.roles,
            allowed_tools=intersected,
            allow_write=self.allow_write and allow_write,
            authenticated=self.authenticated,
            attrs=self.attrs,
        )


@dataclass
class SSEEvent:
    """Standard v1 Server-Sent Event envelope pushed to pluggable frontend clients."""

    event: str  # meta | token | subagent_status | tool_status | approval_required | done | cancelled | error
    data: Dict[str, Any]
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    run_id: str = ""
    seq: int = 0
    schema_version: str = PROTOCOL_VERSION

    def to_envelope(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id or str(self.data.get("run_id") or ""),
            "seq": self.seq,
            "type": self.event,
            "timestamp": self.ts_ms,
            "payload": self.data,
        }

    def to_sse_wire(self) -> str:
        body = json.dumps(self.to_envelope(), ensure_ascii=False)
        lines = []
        if self.seq > 0:
            lines.append(f"id: {self.seq}")
        lines.append(f"event: {self.event}")
        lines.append(f"data: {body}")
        return "\n".join(lines) + "\n\n"


ProgressCallback = Callable[[SSEEvent], Awaitable[None]]


@dataclass
class ExecutionContext:
    """Per-request runtime context propagated across LangGraph nodes, tools, and sub-agents."""

    trace_id: str
    thread_id: str
    auth_scope: AuthScope
    request_id: str
    run_id: str = ""
    op_key: Optional[str] = None
    agent_name: str = "main_agent"
    model_name: str = "deepseek-chat"
    depth: int = 0
    max_depth: int = 1
    parent_span_id: Optional[str] = None
    policy_rev: int = 1
    on_event: Optional[ProgressCallback] = None
    is_cancelled: Optional[Callable[[], bool]] = None

    def check_cancelled(self) -> bool:
        return bool(self.is_cancelled and self.is_cancelled())

    async def emit(self, event: str, data: Dict[str, Any]) -> None:
        if self.on_event is not None:
            await self.on_event(SSEEvent(event=event, data=data, run_id=self.run_id))

    def derive_subagent(
        self,
        *,
        agent_name: str,
        allowed_tools: Tuple[str, ...],
        parent_span_id: str,
        model_name: Optional[str] = None,
    ) -> "ExecutionContext":
        return ExecutionContext(
            trace_id=self.trace_id,
            thread_id=f"{self.thread_id}:sub:{agent_name}",
            auth_scope=self.auth_scope.narrowed(
                allowed_tools=allowed_tools,
                allow_write=False,
            ),
            request_id=self.request_id,
            run_id=self.run_id,
            agent_name=agent_name,
            model_name=model_name or self.model_name,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            parent_span_id=parent_span_id,
            policy_rev=self.policy_rev,
            on_event=self.on_event,
            is_cancelled=self.is_cancelled,
        )

"""Runtime configuration, identity extraction, and request-scoped SSE context."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import os
from typing import Optional
from uuid import uuid4

from agent_runtime.framework.core.contracts import AuthScope, Message, SSEEvent, ToolCall

DEFAULT_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
BUCKET = os.getenv("MINIO_BUCKET", "agent-artifacts")
MAX_DOCUMENT = 2 * 1024 * 1024

sse_sink: ContextVar = ContextVar("sse_sink", default=None)
sse_meta: ContextVar = ContextVar("sse_meta", default=None)


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def resolve_identity(req, default_scope: Optional[AuthScope] = None) -> AuthScope:
    """Extract caller AuthScope from gateway headers, falling back to runtime/plugin default."""
    base = default_scope or AuthScope("default", "local_user", ("user",))
    if "X-User-Id" in req.headers:
        roles = tuple(
            r.strip()
            for r in req.headers.get("X-User-Roles", ",".join(base.roles)).split(",")
            if r.strip()
        )
        return AuthScope(req.headers.get("X-Tenant-Id", base.tenant_id), req.headers["X-User-Id"], roles)
    return base


SSE_PUT_TIMEOUT_SEC = 0.5


async def publish_sse(event: SSEEvent, *, put_timeout: float = SSE_PUT_TIMEOUT_SEC) -> None:
    queue = sse_sink.get()
    if queue is None:
        return
    meta = sse_meta.get()
    if isinstance(meta, dict):
        if meta.get("closed"):
            return
        meta["seq"] = meta.get("seq", 0) + 1
        event.seq = meta["seq"]
        if not event.run_id:
            event.run_id = str(meta.get("run_id") or "")
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass
    try:
        await asyncio.wait_for(queue.put(event), timeout=put_timeout)
    except asyncio.TimeoutError:
        # Slow or disconnected consumer: detach sink cleanly without silently dropping individual tokens
        if isinstance(meta, dict):
            meta["closed"] = True
        else:
            sse_sink.set(None)


def unpack_message(data: dict) -> Message:
    return Message(**{**data, "tool_calls": [ToolCall(**tc) for tc in data.get("tool_calls", [])]})


@dataclass(frozen=True)
class ModelSettings:
    api_key: str
    base_url: str = DEFAULT_BASE
    model: str = DEFAULT_MODEL
    mode: str = "deepseek"

    @property
    def effective(self) -> str:
        return self.mode or "deepseek"

    def public(self) -> dict:
        return {
            "mode": self.effective,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": bool(self.api_key),
        }

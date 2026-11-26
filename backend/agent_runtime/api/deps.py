"""Shared FastAPI request helpers."""

from __future__ import annotations

from fastapi import Request

from agent_runtime.framework.core.config import resolve_identity
from agent_runtime.framework.core.contracts import AuthScope


def caller_scope(req: Request) -> AuthScope:
    return resolve_identity(req, default_scope=req.app.state.runtime.default_identity())

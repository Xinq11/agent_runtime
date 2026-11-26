"""Shared FastAPI request identity & fail-closed authorization helpers."""

from __future__ import annotations

import inspect
from typing import Optional

from fastapi import HTTPException, Request

from agent_runtime.framework.core.contracts import AuthScope
from agent_runtime.framework.core.security import (
    AccessControlError,
    AccessDecision,
    AccessFilter,
    AuthenticationError,
    AuthorizationError,
    HeaderIdentityResolver,
    ResourceRef,
    enforce_check,
    enforce_filter,
    enforce_filter_sync,
)


def _get_resolver(req: Request):
    resolver = getattr(req.app.state, "identity_resolver", None)
    if resolver is not None:
        return resolver
    runtime = getattr(req.app.state, "runtime", None)
    default_scope = runtime.default_identity() if runtime is not None else None
    return HeaderIdentityResolver(default_scope=default_scope, trust_headers=True)


def caller_scope(req: Request) -> AuthScope:
    """Resolve caller identity synchronously (used by sync FastAPI threadpool routes)."""
    resolver = _get_resolver(req)
    try:
        resolved = resolver.resolve(req)
        if inspect.isawaitable(resolved):
            resolved.close()  # type: ignore[union-attr]
            raise AuthenticationError("async_identity_resolver_in_sync_route")
        if not isinstance(resolved, AuthScope):
            raise AuthenticationError("invalid_resolved_identity")
        return resolved
    except AuthenticationError as exc:
        raise HTTPException(401, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(401, f"identity_resolver_error:{type(exc).__name__}") from exc


async def caller_scope_async(req: Request) -> AuthScope:
    """Resolve caller identity asynchronously (supports both sync and async IdentityResolver)."""
    resolver = _get_resolver(req)
    try:
        resolved = resolver.resolve(req)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        if not isinstance(resolved, AuthScope):
            raise AuthenticationError("invalid_resolved_identity")
        return resolved
    except AuthenticationError as exc:
        raise HTTPException(401, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(401, f"identity_resolver_error:{type(exc).__name__}") from exc


async def require_access_async(
    req: Request,
    scope: AuthScope,
    action: str,
    resource: ResourceRef,
) -> AccessDecision:
    """Fail-closed authorization gate for async FastAPI routes."""
    ac = req.app.state.runtime.access_controller
    try:
        return await enforce_check(ac, scope, action, resource)
    except AuthorizationError as exc:
        raise HTTPException(exc.status_code, exc.reason) from exc
    except AccessControlError as exc:
        raise HTTPException(503, str(exc)) from exc


def require_access(
    req: Request,
    scope: AuthScope,
    action: str,
    resource: ResourceRef,
) -> AccessDecision:
    """Fail-closed authorization gate for sync FastAPI routes."""
    ac = req.app.state.runtime.access_controller
    try:
        decision = ac.check(scope, action, resource)
        if inspect.isawaitable(decision):
            decision.close()  # type: ignore[union-attr]
            raise AccessControlError("async_access_controller_in_sync_route")
        if not isinstance(decision, AccessDecision):
            raise AccessControlError("invalid_access_decision")
        if not decision.allowed:
            status = 404 if decision.code == "not_found" else 403
            raise HTTPException(status, decision.reason or "permission_denied")
        return decision
    except HTTPException:
        raise
    except AuthorizationError as exc:
        raise HTTPException(exc.status_code, exc.reason) from exc
    except AccessControlError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, f"access_controller_error:{type(exc).__name__}") from exc


async def resolve_filter_async(
    req: Request,
    scope: AuthScope,
    action: str,
    resource_kind: str,
) -> AccessFilter:
    ac = req.app.state.runtime.access_controller
    try:
        return await enforce_filter(ac, scope, action, resource_kind)
    except AccessControlError as exc:
        raise HTTPException(503, str(exc)) from exc


def resolve_filter(
    req: Request,
    scope: AuthScope,
    action: str,
    resource_kind: str,
) -> AccessFilter:
    ac = req.app.state.runtime.access_controller
    try:
        return enforce_filter_sync(ac, scope, action, resource_kind)
    except AccessControlError as exc:
        raise HTTPException(503, str(exc)) from exc

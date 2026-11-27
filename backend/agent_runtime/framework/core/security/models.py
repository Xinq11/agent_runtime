"""Minimal security exceptions, AuthN/AuthZ point-check protocols, and fail-closed enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import inspect
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from agent_runtime.framework.core.contracts import AuthScope


class AuthenticationError(RuntimeError):
    """Raised when caller credentials are missing, malformed, expired, or invalid (HTTP 401)."""


class AuthorizationError(PermissionError):
    """Raised when an authenticated/resolved caller is not permitted to perform an action."""

    def __init__(self, reason: str = "permission_denied", *, code: str = "forbidden", status_code: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.status_code = status_code


class AccessControlError(RuntimeError):
    """Raised on fail-closed access controller failure (timeout, backend error, invalid decision)."""


@dataclass(frozen=True)
class ResourceRef:
    """Structured target reference passed to `AccessController.check`."""

    kind: str  # "approval" | "tool" | "artifact" | "system"
    id: str = "*"
    owner_id: Optional[str] = None
    attrs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccessDecision:
    """Point authorization decision returned by `AccessController.check`."""

    allowed: bool
    reason: str = ""
    code: str = "forbidden"  # "forbidden" | "not_found"
    policy_rev: int = 1


@runtime_checkable
class IdentityResolver(Protocol):
    """AuthN protocol: resolves an incoming HTTP request or host context into an `AuthScope`."""

    def resolve(self, context: Any) -> Any: ...


@runtime_checkable
class AccessController(Protocol):
    """Minimal AuthZ point-check protocol for tools, approvals, artifacts, and model updates."""

    def check(self, scope: AuthScope, action: str, resource: ResourceRef) -> Any: ...

    def policy_rev(self, scope: AuthScope) -> int: ...


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def import_symbol(spec: str) -> Any:
    """Dynamically load a `module.path:Symbol` implementation."""
    if ":" not in spec:
        raise ValueError(f"Invalid symbol spec '{spec}'; expected 'module.path:Symbol'")
    mod_name, attr_name = spec.split(":", 1)
    mod = importlib.import_module(mod_name)
    obj = getattr(mod, attr_name)
    return obj() if callable(obj) else obj


async def resolve_identity_async(resolver: IdentityResolver, context: Any) -> AuthScope:
    try:
        scope = await _maybe_await(resolver.resolve(context))
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError(f"identity_resolver_error:{type(exc).__name__}") from exc
    if not isinstance(scope, AuthScope):
        raise AuthenticationError("invalid_resolved_identity")
    return scope


async def enforce_check(
    controller: AccessController,
    scope: AuthScope,
    action: str,
    resource: ResourceRef,
) -> AccessDecision:
    """Fail-closed point authorization check."""
    try:
        decision = await _maybe_await(controller.check(scope, action, resource))
    except (AuthorizationError, AccessControlError):
        raise
    except Exception as exc:
        raise AccessControlError(f"access_controller_error:{type(exc).__name__}") from exc
    if not isinstance(decision, AccessDecision):
        raise AccessControlError("invalid_access_decision")
    if not decision.allowed:
        status = 404 if decision.code == "not_found" else 403
        raise AuthorizationError(decision.reason or "permission_denied", code=decision.code, status_code=status)
    return decision


def get_policy_rev(controller: Optional[AccessController], scope: AuthScope) -> int:
    if controller is None:
        return 1
    try:
        rev = controller.policy_rev(scope)
        return int(rev)
    except Exception as exc:
        raise AccessControlError(f"policy_rev_error:{type(exc).__name__}") from exc

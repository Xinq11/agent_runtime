"""Pluggable authentication (`IdentityResolver`) & authorization (`AccessController`) primitives.

Design invariants:
  - AuthN (`IdentityResolver`) and AuthZ (`AccessController`) are strictly decoupled.
  - Enterprise mode is strictly fail-closed: controller errors raise `AccessControlError`
    and never silently fall back to permissive behavior.
  - Core storage invariants (`tenant_id` isolation, private thread ownership, artifact owner/TTL,
    approval atomicity/idempotency, subagent monotonic narrowing) remain enforced regardless
    of which `AccessController` is installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import inspect
import json
import os
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable

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
    """Raised on fail-closed access controller failure (timeout, backend error, invalid filter)."""


@dataclass(frozen=True)
class ResourceRef:
    """Structured target reference passed to `AccessController.check`."""

    kind: str  # "thread" | "run" | "approval" | "tool" | "rag_doc" | "skill" | "artifact" | "ops" | "system"
    id: str = "*"
    tenant_id: Optional[str] = None
    owner_id: Optional[str] = None
    attrs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccessDecision:
    """Point authorization decision returned by `AccessController.check`."""

    allowed: bool
    reason: str = ""
    code: str = "forbidden"  # "forbidden" | "not_found"
    policy_rev: int = 1


@dataclass(frozen=True)
class AccessFilter:
    """Structured whitelist predicate pushed down to PG SQL or external retrieval engines.

    Plugins never emit raw SQL strings; storage adapters compile only whitelisted fields.
    """

    tenant_id: str
    unrestricted: bool = False
    any_roles: Tuple[str, ...] = ()
    any_depts: Tuple[str, ...] = ()
    owner_id: Optional[str] = None
    include_public: bool = True
    include_disabled: bool = False
    deny_all: bool = False
    policy_rev: int = 1

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("AccessFilter.tenant_id is required")
        object.__setattr__(self, "any_roles", tuple(self.any_roles or ()))
        object.__setattr__(self, "any_depts", tuple(self.any_depts or ()))


@runtime_checkable
class IdentityResolver(Protocol):
    """AuthN protocol: resolves an incoming HTTP request or host context into an `AuthScope`."""

    def resolve(self, context: Any) -> Any: ...


@runtime_checkable
class AccessController(Protocol):
    """AuthZ protocol: point action checks, SQL-pushable data filters, and policy revision."""

    def check(self, scope: AuthScope, action: str, resource: ResourceRef) -> Any: ...

    def filter_for(self, scope: AuthScope, action: str, resource_kind: str) -> Any: ...

    def policy_rev(self, scope: AuthScope) -> int: ...


class StaticIdentityResolver:
    """Ignores untrusted request headers and always returns a fixed deployment default identity."""

    def __init__(self, default_scope: Optional[AuthScope] = None) -> None:
        self._scope = default_scope or AuthScope("default", "local_user", ("user",))

    def resolve(self, context: Any) -> AuthScope:
        return self._scope


class HeaderIdentityResolver:
    """Resolves identity from trusted upstream gateway headers (with optional HMAC verification).

    When `hmac_secret` is configured, requires `X-Tenant-Id`, `X-User-Id`, and a valid
    `X-Gateway-Signature` produced with `itsdangerous.TimestampSigner(hmac_secret)` over
    `"{tenant_id}|{user_id}|{roles_csv}"`.
    """

    def __init__(
        self,
        *,
        default_scope: Optional[AuthScope] = None,
        trust_headers: bool = True,
        hmac_secret: Optional[str] = None,
        max_age_sec: int = 300,
    ) -> None:
        self._default_scope = default_scope or AuthScope("default", "local_user", ("user",))
        self._trust_headers = trust_headers
        self._hmac_secret = (hmac_secret or "").strip() or None
        self._max_age_sec = max_age_sec

    @staticmethod
    def _validate_identity_field(name: str, value: str) -> str:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise AuthenticationError(f"invalid_{name}")
        cleaned = value.strip()
        if not cleaned:
            raise AuthenticationError(f"invalid_{name}")
        return cleaned

    @staticmethod
    def _encode_signed_payload(tenant_id: str, user_id: str, roles: Tuple[str, ...]) -> str:
        return json.dumps(["v1", tenant_id, user_id, list(roles)], separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def sign_headers(
        *,
        secret: str,
        tenant_id: str,
        user_id: str,
        roles: Tuple[str, ...] = (),
    ) -> Dict[str, str]:
        from itsdangerous import TimestampSigner

        clean_tenant = HeaderIdentityResolver._validate_identity_field("tenant_id", tenant_id)
        clean_user = HeaderIdentityResolver._validate_identity_field("user_id", user_id)
        clean_roles: list[str] = []
        for raw_role in roles:
            r = HeaderIdentityResolver._validate_identity_field("role", str(raw_role))
            if "," in r:
                raise AuthenticationError("invalid_role_delimiter")
            clean_roles.append(r)
        roles_tuple = tuple(clean_roles)
        payload = HeaderIdentityResolver._encode_signed_payload(clean_tenant, clean_user, roles_tuple)
        sig = TimestampSigner(secret).sign(payload).decode("utf-8")
        return {
            "X-Tenant-Id": clean_tenant,
            "X-User-Id": clean_user,
            "X-User-Roles": ",".join(roles_tuple),
            "X-Gateway-Signature": sig,
        }

    def resolve(self, context: Any) -> AuthScope:
        headers = getattr(context, "headers", context) if context is not None else {}
        if self._hmac_secret:
            from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

            raw_tenant = str(headers.get("X-Tenant-Id") or "")
            raw_user = str(headers.get("X-User-Id") or "")
            raw_roles = str(headers.get("X-User-Roles") or "")
            signature = str(headers.get("X-Gateway-Signature") or "").strip()
            if not raw_tenant.strip() or not raw_user.strip() or not signature:
                raise AuthenticationError("missing_gateway_signature")
            tenant_id = self._validate_identity_field("tenant_id", raw_tenant)
            user_id = self._validate_identity_field("user_id", raw_user)
            if raw_roles.strip():
                parts = raw_roles.split(",")
                roles = tuple(self._validate_identity_field("role", p) for p in parts)
            else:
                roles = ()
            expected = self._encode_signed_payload(tenant_id, user_id, roles)
            try:
                unsigned = TimestampSigner(self._hmac_secret).unsign(
                    signature, max_age=self._max_age_sec
                ).decode("utf-8")
            except (BadSignature, SignatureExpired) as exc:
                raise AuthenticationError("invalid_gateway_signature") from exc
            if unsigned != expected:
                raise AuthenticationError("gateway_signature_mismatch")
            return AuthScope(tenant_id=tenant_id, user_id=user_id, roles=roles, authenticated=True)

        if not self._trust_headers or "X-User-Id" not in headers:
            return self._default_scope

        raw_roles = headers.get("X-User-Roles")
        if raw_roles is None:
            roles = self._default_scope.roles
        else:
            roles = tuple(r.strip() for r in str(raw_roles).split(",") if r.strip())
        tenant_id = str(headers.get("X-Tenant-Id") or self._default_scope.tenant_id).strip()
        user_id = str(headers["X-User-Id"]).strip()
        if not user_id:
            raise AuthenticationError("empty_user_id")
        return AuthScope(tenant_id=tenant_id, user_id=user_id, roles=roles, authenticated=True)


class NoopAccessController:
    """Explicit single-permission deployment policy (all actions allowed within tenant)."""

    def __init__(self, *, policy_rev: int = 1) -> None:
        self._policy_rev = policy_rev

    def check(self, scope: AuthScope, action: str, resource: ResourceRef) -> AccessDecision:
        if resource.tenant_id and resource.tenant_id != scope.tenant_id:
            return AccessDecision(False, "cross_tenant_denied", code="not_found", policy_rev=self._policy_rev)
        return AccessDecision(True, policy_rev=self._policy_rev)

    def filter_for(self, scope: AuthScope, action: str, resource_kind: str) -> AccessFilter:
        return AccessFilter(
            tenant_id=scope.tenant_id,
            unrestricted=True,
            include_public=True,
            include_disabled=action.endswith(":manage"),
            policy_rev=self._policy_rev,
        )

    def policy_rev(self, scope: AuthScope) -> int:
        return self._policy_rev


class RbacAccessController:
    """Built-in role-based access controller supporting both local default and enterprise RBAC.

    - `admin_roles=None` (default single-role mode): management routes are open within tenant,
      while RAG/Skills still strictly enforce resource `acl_roles` (with fixed empty-role semantics).
    - `admin_roles=("admin", ...)` (enterprise RBAC mode): management, ops, model config, disabled
      skill previews, and optional per-tool / approver roles are strictly gated by caller roles.
    """

    _TENANT_ADMIN_ACTIONS = frozenset({"ops:view", "kb:manage", "skill:manage"})
    _PLATFORM_ADMIN_ACTIONS = frozenset({"system:update_model", "platform:view_telemetry"})

    def __init__(
        self,
        *,
        admin_roles: Optional[Tuple[str, ...]] = ("admin",),
        platform_admin_roles: Optional[Tuple[str, ...]] = ("platform_admin",),
        approver_roles: Optional[Tuple[str, ...]] = None,
        forbid_self_approval: bool = False,
        tool_roles: Optional[Mapping[str, Tuple[str, ...]]] = None,
        policy_rev: int = 1,
    ) -> None:
        self.admin_roles = None if admin_roles is None else tuple(admin_roles)
        self.platform_admin_roles = (
            None
            if (admin_roles is None or platform_admin_roles is None)
            else tuple(platform_admin_roles)
        )
        self.approver_roles = None if approver_roles is None else tuple(approver_roles)
        self.forbid_self_approval = forbid_self_approval
        self.tool_roles = {k: tuple(v) for k, v in (tool_roles or {}).items()}
        self._policy_rev = policy_rev

    def _is_platform_admin(self, scope: AuthScope) -> bool:
        if self.platform_admin_roles is None:
            return True
        return bool(set(scope.roles) & set(self.platform_admin_roles))

    def _is_admin(self, scope: AuthScope) -> bool:
        if self.admin_roles is None:
            return True
        return self._is_platform_admin(scope) or bool(set(scope.roles) & set(self.admin_roles))

    def bump_policy_rev(self) -> int:
        self._policy_rev += 1
        return self._policy_rev

    def policy_rev(self, scope: AuthScope) -> int:
        return self._policy_rev

    def check(self, scope: AuthScope, action: str, resource: ResourceRef) -> AccessDecision:
        if resource.tenant_id and resource.tenant_id != scope.tenant_id:
            return AccessDecision(False, "cross_tenant_denied", code="not_found", policy_rev=self._policy_rev)

        if action in self._PLATFORM_ADMIN_ACTIONS:
            if not self._is_platform_admin(scope):
                return AccessDecision(
                    False, "platform_admin_role_required", code="forbidden", policy_rev=self._policy_rev
                )
            return AccessDecision(True, policy_rev=self._policy_rev)

        if action in self._TENANT_ADMIN_ACTIONS:
            if not self._is_admin(scope):
                return AccessDecision(False, "admin_role_required", code="forbidden", policy_rev=self._policy_rev)
            return AccessDecision(True, policy_rev=self._policy_rev)

        if action == "approval:decide":
            if self.forbid_self_approval and resource.owner_id and resource.owner_id == scope.user_id:
                return AccessDecision(
                    False, "self_approval_forbidden", code="forbidden", policy_rev=self._policy_rev
                )
            if self.approver_roles is not None:
                if not (set(scope.roles) & set(self.approver_roles)):
                    return AccessDecision(
                        False, "approver_role_required", code="forbidden", policy_rev=self._policy_rev
                    )
                return AccessDecision(True, policy_rev=self._policy_rev)
            # Without explicit approver_roles: requester or admin may decide
            if resource.owner_id and resource.owner_id != scope.user_id and not self._is_admin(scope):
                return AccessDecision(
                    False, "approval_not_authorized", code="not_found", policy_rev=self._policy_rev
                )
            return AccessDecision(True, policy_rev=self._policy_rev)

        if action in ("tool:list", "tool:invoke"):
            required = self.tool_roles.get(resource.id)
            if required and not (set(scope.roles) & set(required)):
                return AccessDecision(
                    False, "tool_role_required", code="forbidden", policy_rev=self._policy_rev
                )
            return AccessDecision(True, policy_rev=self._policy_rev)

        return AccessDecision(True, policy_rev=self._policy_rev)

    def filter_for(self, scope: AuthScope, action: str, resource_kind: str) -> AccessFilter:
        if action in self._TENANT_ADMIN_ACTIONS:
            if not self._is_admin(scope):
                return AccessFilter(tenant_id=scope.tenant_id, deny_all=True, policy_rev=self._policy_rev)
            return AccessFilter(
                tenant_id=scope.tenant_id,
                unrestricted=True,
                include_public=True,
                include_disabled=True,
                policy_rev=self._policy_rev,
            )
        return AccessFilter(
            tenant_id=scope.tenant_id,
            unrestricted=False,
            any_roles=scope.roles,
            include_public=True,
            include_disabled=False,
            policy_rev=self._policy_rev,
        )


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


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


async def enforce_filter(
    controller: AccessController,
    scope: AuthScope,
    action: str,
    resource_kind: str,
) -> AccessFilter:
    """Fail-closed data filter builder enforcing permanent tenant_id invariant."""
    try:
        flt = await _maybe_await(controller.filter_for(scope, action, resource_kind))
    except AccessControlError:
        raise
    except Exception as exc:
        raise AccessControlError(f"access_filter_error:{type(exc).__name__}") from exc
    if not isinstance(flt, AccessFilter) or flt.tenant_id != scope.tenant_id:
        raise AccessControlError("invalid_access_filter")
    return flt


def enforce_filter_sync(
    controller: AccessController,
    scope: AuthScope,
    action: str,
    resource_kind: str,
) -> AccessFilter:
    """Synchronous variant for sync call paths; rejects awaitable controllers fail-closed."""
    try:
        flt = controller.filter_for(scope, action, resource_kind)
    except AccessControlError:
        raise
    except Exception as exc:
        raise AccessControlError(f"access_filter_error:{type(exc).__name__}") from exc
    if inspect.isawaitable(flt):
        flt.close()  # type: ignore[union-attr]
        raise AccessControlError("async_access_filter_in_sync_path")
    if not isinstance(flt, AccessFilter) or flt.tenant_id != scope.tenant_id:
        raise AccessControlError("invalid_access_filter")
    return flt


def get_policy_rev(controller: Optional[AccessController], scope: AuthScope) -> int:
    if controller is None:
        return 1
    try:
        rev = controller.policy_rev(scope)
        return int(rev)
    except Exception as exc:
        raise AccessControlError(f"policy_rev_error:{type(exc).__name__}") from exc


def _import_symbol(spec: str) -> Any:
    if ":" not in spec:
        raise ValueError(f"Invalid symbol spec '{spec}'; expected 'module.path:Symbol'")
    mod_name, attr_name = spec.split(":", 1)
    mod = importlib.import_module(mod_name)
    obj = getattr(mod, attr_name)
    return obj() if callable(obj) else obj


def load_access_controller(spec: Optional[str] = None) -> AccessController:
    """Load an `AccessController` from env/config: `default` | `noop` | `rbac` | `module:Symbol`."""
    raw = (spec if spec is not None else os.getenv("AGENT_ACCESS_CONTROLLER", "default")).strip()
    low = raw.lower()
    if not low or low == "default":
        return RbacAccessController(admin_roles=None)
    if low == "noop":
        return NoopAccessController()
    if low == "rbac":
        admins = tuple(
            r.strip() for r in os.getenv("AGENT_ADMIN_ROLES", "admin").split(",") if r.strip()
        )
        platform_admins = tuple(
            r.strip()
            for r in os.getenv("AGENT_PLATFORM_ADMIN_ROLES", "platform_admin").split(",")
            if r.strip()
        )
        approvers_raw = os.getenv("AGENT_APPROVER_ROLES", "").strip()
        approvers = (
            tuple(r.strip() for r in approvers_raw.split(",") if r.strip()) if approvers_raw else None
        )
        forbid_self = os.getenv("AGENT_FORBID_SELF_APPROVAL", "").strip().lower() in ("1", "true", "yes")
        return RbacAccessController(
            admin_roles=admins or ("admin",),
            platform_admin_roles=platform_admins or ("platform_admin",),
            approver_roles=approvers,
            forbid_self_approval=forbid_self,
        )
    inst = _import_symbol(raw)
    if not isinstance(inst, AccessController):
        raise TypeError(f"Loaded access controller '{raw}' does not implement AccessController protocol")
    return inst


def load_identity_resolver(
    spec: Optional[str] = None,
    *,
    default_scope: Optional[AuthScope] = None,
) -> IdentityResolver:
    """Load an `IdentityResolver` from env/config: `header` | `default` | `signed_header` | `module:Symbol`."""
    raw = (spec if spec is not None else os.getenv("AGENT_IDENTITY_RESOLVER", "header")).strip()
    low = raw.lower()
    hmac_secret = os.getenv("AGENT_GATEWAY_HMAC_SECRET", "").strip() or None
    if low == "default":
        return StaticIdentityResolver(default_scope=default_scope)
    if low == "signed_header":
        if not hmac_secret:
            raise ValueError("AGENT_GATEWAY_HMAC_SECRET is required when AGENT_IDENTITY_RESOLVER=signed_header")
        return HeaderIdentityResolver(default_scope=default_scope, trust_headers=True, hmac_secret=hmac_secret)
    if not low or low == "header":
        return HeaderIdentityResolver(default_scope=default_scope, trust_headers=True, hmac_secret=hmac_secret)
    inst = _import_symbol(raw)
    if not isinstance(inst, IdentityResolver):
        raise TypeError(f"Loaded identity resolver '{raw}' does not implement IdentityResolver protocol")
    return inst

"""Built-in `AccessController` implementations (`Noop`, `Rbac`) and environment loader."""

from __future__ import annotations

import os
from typing import Mapping, Optional, Tuple

from agent_runtime.framework.core.contracts import AuthScope
from agent_runtime.framework.core.security.models import (
    AccessController,
    AccessDecision,
    AccessFilter,
    ResourceRef,
    import_symbol,
)


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
    - `admin_roles=("admin", ...)` (enterprise RBAC mode): tenant management, platform telemetry/model
      writes, disabled skill previews, and optional per-tool / approver roles are strictly gated.
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
    inst = import_symbol(raw)
    if not isinstance(inst, AccessController):
        raise TypeError(f"Loaded access controller '{raw}' does not implement AccessController protocol")
    return inst

"""Pluggable authentication (`IdentityResolver`) & authorization (`AccessController`) package."""

from __future__ import annotations

from agent_runtime.framework.core.security.identity import (
    HeaderIdentityResolver,
    StaticIdentityResolver,
    load_identity_resolver,
)
from agent_runtime.framework.core.security.models import (
    AccessControlError,
    AccessController,
    AccessDecision,
    AccessFilter,
    AuthenticationError,
    AuthorizationError,
    IdentityResolver,
    ResourceRef,
    enforce_check,
    enforce_filter,
    enforce_filter_sync,
    get_policy_rev,
    resolve_identity_async,
)
from agent_runtime.framework.core.security.rbac import (
    NoopAccessController,
    RbacAccessController,
    load_access_controller,
)

__all__ = [
    "AccessControlError",
    "AccessController",
    "AccessDecision",
    "AccessFilter",
    "AuthenticationError",
    "AuthorizationError",
    "HeaderIdentityResolver",
    "IdentityResolver",
    "NoopAccessController",
    "RbacAccessController",
    "ResourceRef",
    "StaticIdentityResolver",
    "enforce_check",
    "enforce_filter",
    "enforce_filter_sync",
    "get_policy_rev",
    "load_access_controller",
    "load_identity_resolver",
    "resolve_identity_async",
]

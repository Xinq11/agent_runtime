"""Parameterized SQL compiler for whitelisted `AccessFilter` predicates."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from agent_runtime.framework.core.contracts import AuthScope
from agent_runtime.framework.core.security import AccessControlError, AccessFilter

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _safe_col(alias: str, col: str) -> str:
    if not _IDENT_RE.match(col):
        raise ValueError(f"Invalid SQL column identifier: {col}")
    if alias:
        if not _IDENT_RE.match(alias):
            raise ValueError(f"Invalid SQL alias identifier: {alias}")
        return f"{alias}.{col}"
    return col


def default_access_filter(
    scope: AuthScope,
    *,
    unrestricted: bool = False,
    include_disabled: bool = False,
) -> AccessFilter:
    """Construct a safe default `AccessFilter` when storage methods are called directly."""
    return AccessFilter(
        tenant_id=scope.tenant_id,
        unrestricted=unrestricted,
        any_roles=scope.roles,
        include_public=True,
        include_disabled=include_disabled,
    )


def compile_acl_sql(
    flt: AccessFilter,
    *,
    scope: Optional[AuthScope] = None,
    alias: str = "",
    tenant_col: str = "tenant_id",
    roles_col: str = "acl_roles",
    status_col: Optional[str] = "status",
    owner_col: Optional[str] = None,
    depts_col: Optional[str] = None,
    param_prefix: str = "acl",
) -> Tuple[str, Dict[str, Any]]:
    """Compile an `AccessFilter` into a parameterized SQL WHERE fragment.

    Fail-closed invariants:
      - Rejects tenant mismatch when `scope` is supplied.
      - Rejects unsupported `owner_id` / `any_depts` predicates rather than silently widening.
      - Always binds `tenant_id`.
      - Empty `any_roles` never bypasses `acl_roles`; matches only public (`cardinality=0`)
        when `include_public=True`, or `FALSE` when `include_public=False`.
    """
    if scope is not None and flt.tenant_id != scope.tenant_id:
        raise AccessControlError("access_filter_tenant_mismatch")

    tcol = _safe_col(alias, tenant_col)
    rcol = _safe_col(alias, roles_col)
    clauses = [f"{tcol}=%({param_prefix}_tenant)s"]
    params: Dict[str, Any] = {f"{param_prefix}_tenant": flt.tenant_id}

    if status_col and not flt.include_disabled:
        scol = _safe_col(alias, status_col)
        clauses.append(f"{scol}='active'")

    if flt.deny_all:
        clauses.append("FALSE")
        return " AND ".join(clauses), params

    if flt.owner_id is not None:
        if not owner_col:
            raise AccessControlError("unsupported_access_filter:owner_id")
        ocol = _safe_col(alias, owner_col)
        clauses.append(f"{ocol}=%({param_prefix}_owner)s")
        params[f"{param_prefix}_owner"] = flt.owner_id

    if flt.any_depts:
        if not depts_col:
            raise AccessControlError("unsupported_access_filter:any_depts")
        dcol = _safe_col(alias, depts_col)
        clauses.append(f"{dcol} && %({param_prefix}_depts)s::text[]")
        params[f"{param_prefix}_depts"] = list(flt.any_depts)

    if not flt.unrestricted:
        if flt.any_roles:
            params[f"{param_prefix}_roles"] = list(flt.any_roles)
            if flt.include_public:
                clauses.append(f"(cardinality({rcol})=0 OR {rcol} && %({param_prefix}_roles)s::text[])")
            else:
                clauses.append(f"({rcol} && %({param_prefix}_roles)s::text[])")
        else:
            if flt.include_public:
                clauses.append(f"cardinality({rcol})=0")
            else:
                clauses.append("FALSE")

    return " AND ".join(clauses), params

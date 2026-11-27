"""Built-in `IdentityResolver` implementations and environment loader."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from agent_runtime.framework.core.contracts import AuthScope
from agent_runtime.framework.core.security.models import (
    AuthenticationError,
    IdentityResolver,
    import_symbol,
)


class StaticIdentityResolver:
    """Ignores untrusted request headers and always returns a fixed deployment default identity."""

    def __init__(self, default_scope: Optional[AuthScope] = None) -> None:
        self._scope = default_scope or AuthScope("local_user", ("user",))

    def resolve(self, context: Any) -> AuthScope:
        return self._scope


class HeaderIdentityResolver:
    """Resolves trusted caller `user_id` (+ optional `roles`) from upstream gateway headers.

    When `hmac_secret` is configured, requires `X-User-Id` and a valid `X-Gateway-Signature`
    produced with `itsdangerous.TimestampSigner(hmac_secret)` over the canonical JSON payload
    `["v1", user_id, roles]`. Any legacy `X-Tenant-Id` header is ignored.
    """

    def __init__(
        self,
        *,
        default_scope: Optional[AuthScope] = None,
        trust_headers: bool = True,
        hmac_secret: Optional[str] = None,
        max_age_sec: int = 300,
    ) -> None:
        self._default_scope = default_scope or AuthScope("local_user", ("user",))
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
    def _encode_signed_payload(user_id: str, roles: Tuple[str, ...]) -> str:
        return json.dumps(["v1", user_id, list(roles)], separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def sign_headers(
        *,
        secret: str,
        user_id: str,
        roles: Tuple[str, ...] = (),
    ) -> Dict[str, str]:
        from itsdangerous import TimestampSigner

        clean_user = HeaderIdentityResolver._validate_identity_field("user_id", user_id)
        clean_roles: list[str] = []
        for raw_role in roles:
            r = HeaderIdentityResolver._validate_identity_field("role", str(raw_role))
            if "," in r:
                raise AuthenticationError("invalid_role_delimiter")
            clean_roles.append(r)
        roles_tuple = tuple(clean_roles)
        payload = HeaderIdentityResolver._encode_signed_payload(clean_user, roles_tuple)
        sig = TimestampSigner(secret).sign(payload).decode("utf-8")
        return {
            "X-User-Id": clean_user,
            "X-User-Roles": ",".join(roles_tuple),
            "X-Gateway-Signature": sig,
        }

    def resolve(self, context: Any) -> AuthScope:
        headers = getattr(context, "headers", context) if context is not None else {}
        if self._hmac_secret:
            from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

            raw_user = str(headers.get("X-User-Id") or "")
            raw_roles = str(headers.get("X-User-Roles") or "")
            signature = str(headers.get("X-Gateway-Signature") or "").strip()
            if not raw_user.strip() or not signature:
                raise AuthenticationError("missing_gateway_signature")
            user_id = self._validate_identity_field("user_id", raw_user)
            if raw_roles.strip():
                parts = raw_roles.split(",")
                roles = tuple(self._validate_identity_field("role", p) for p in parts)
            else:
                roles = ()
            expected = self._encode_signed_payload(user_id, roles)
            try:
                unsigned = TimestampSigner(self._hmac_secret).unsign(
                    signature, max_age=self._max_age_sec
                ).decode("utf-8")
            except (BadSignature, SignatureExpired) as exc:
                raise AuthenticationError("invalid_gateway_signature") from exc
            if unsigned != expected:
                raise AuthenticationError("gateway_signature_mismatch")
            return AuthScope(user_id=user_id, roles=roles, authenticated=True)

        if not self._trust_headers or "X-User-Id" not in headers:
            return self._default_scope

        raw_roles = headers.get("X-User-Roles")
        if raw_roles is None:
            roles = self._default_scope.roles
        else:
            roles = tuple(r.strip() for r in str(raw_roles).split(",") if r.strip())
        user_id = str(headers["X-User-Id"]).strip()
        if not user_id:
            raise AuthenticationError("empty_user_id")
        return AuthScope(user_id=user_id, roles=roles, authenticated=True)


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
    inst = import_symbol(raw)
    if not isinstance(inst, IdentityResolver):
        raise TypeError(f"Loaded identity resolver '{raw}' does not implement IdentityResolver protocol")
    return inst

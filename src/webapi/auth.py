"""Authentication modes: local, identity-aware proxy, and OIDC bearer tokens.

`local` remains the one-workstation developer convenience. `trusted_proxy` is
the Phase 3 behaviour, unchanged. `oidc` is the Phase 4 addition: the API
verifies the token itself against the issuer's published keys.

Whatever the mode, the result is one `Principal` carrying a tenant, an actor and
a set of normalized roles. Authorization is then a separate question answered by
:mod:`src.workspace.rbac`, so no route ever hand-rolls a role check.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from uuid import UUID

from src.workspace.rbac import (
    ROLE_PERMISSIONS,
    AuthorizationError,
    evaluate,
    normalize_role,
    normalize_roles,
    permissions_for,
)


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    actor_id: str
    roles: frozenset[str]
    auth_mode: str = "local"
    email: str | None = None
    display_name: str | None = None
    claims: dict = field(default_factory=dict, repr=False, compare=False)

    def can(self, role_or_permission: str) -> bool:
        """Backwards-compatible check.

        Phase 3 called ``can("reviewer")`` with a role name. Phase 4 routes call
        ``can("review.decide")`` with a permission. Both are answered correctly.
        """
        if "." in role_or_permission:
            return evaluate(self.roles, role_or_permission).allowed
        if role_or_permission in self.roles or "administrator" in self.roles:
            return True
        # A senior role subsumes a junior one. `org_admin` holds every reviewer
        # permission, so can("reviewer") must answer yes rather than compare
        # role names as strings.
        required = ROLE_PERMISSIONS.get(normalize_role(role_or_permission) or "")
        return bool(required) and required <= self.permissions

    def require(self, permission: str) -> None:
        decision = evaluate(self.roles, permission)
        if not decision.allowed:
            raise AuthorizationError(decision.reason)

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for(self.roles)

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "actor_id": self.actor_id,
            "roles": sorted(self.roles),
            "permissions": sorted(self.permissions),
            "auth_mode": self.auth_mode,
            "email": self.email,
            "display_name": self.display_name,
        }


class AuthenticationError(RuntimeError):
    pass


def _tenant(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AuthenticationError("tenant claim must be a UUID") from exc


def _roles(values, *, default: str = "reviewer,operator") -> frozenset[str]:
    resolved = normalize_roles(values if values else default)
    if not resolved:
        raise AuthenticationError("no recognized role was supplied for this account")
    return resolved


def authenticate(
    *,
    mode: str,
    tenant: str | None,
    actor: str | None,
    roles: str | None,
    supplied_proxy_secret: str | None,
    configured_tenant: str,
    configured_proxy_secret: str | None,
    bearer_token: str | None = None,
    verifier=None,
) -> Principal:
    if mode not in {"local", "trusted_proxy", "oidc"}:
        raise AuthenticationError("server authentication mode is invalid")

    if mode == "oidc":
        return _authenticate_oidc(bearer_token, verifier, configured_tenant)

    if mode == "trusted_proxy":
        if (
            not configured_proxy_secret
            or not supplied_proxy_secret
            or not hmac.compare_digest(supplied_proxy_secret, configured_proxy_secret)
        ):
            raise AuthenticationError("identity proxy authentication failed")
        if not tenant or not actor:
            raise AuthenticationError("the identity proxy did not supply tenant and actor claims")
    else:
        tenant = tenant or configured_tenant
        actor = actor or "local-reviewer"

    # `local` is one trusted developer workstation, so it defaults to the full
    # organization-administrator role. `trusted_proxy` defaults conservatively;
    # a real deployment supplies explicit roles from its directory.
    default = "org_admin" if mode == "local" else "reviewer,operator"
    return Principal(
        tenant_id=_tenant(tenant),
        actor_id=str(actor)[:200],
        roles=_roles(roles, default=default),
        auth_mode=mode,
    )


def _authenticate_oidc(bearer_token, verifier, configured_tenant: str) -> Principal:
    if verifier is None:
        raise AuthenticationError("the OIDC verifier is not configured on this server")
    if not bearer_token:
        raise AuthenticationError("a bearer token is required")
    from src.webapi.oidc import OidcError

    try:
        claims = verifier.verify(bearer_token)
    except OidcError as exc:
        raise AuthenticationError(str(exc)) from exc

    tenant = verifier.tenant(claims)
    if not tenant:
        if verifier.settings.require_tenant_claim:
            raise AuthenticationError(
                "the bearer token does not carry the configured organization claim"
            )
        tenant = configured_tenant

    profile = verifier.profile(claims)
    resolved = normalize_roles(verifier.roles(claims))
    if not resolved:
        # An authenticated directory account with no mapped group can still
        # read. It cannot decide, run audits or change anything.
        resolved = frozenset({"viewer"})

    return Principal(
        tenant_id=_tenant(tenant),
        actor_id=str(profile["subject"])[:200],
        roles=resolved,
        auth_mode="oidc",
        email=profile.get("email"),
        display_name=profile.get("display_name"),
        claims=claims,
    )


def bearer_from_header(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "Principal",
    "authenticate",
    "bearer_from_header",
]

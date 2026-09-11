"""Role-based access control for the reviewer workspace.

Phase 3 had two effective roles checked ad hoc at each route. Phase 4 declares
permissions once, derives them from roles, and lets the API ask a single
question: does this principal hold this permission?

Roles are additive. `administrator` is a tenant administrator, not a platform
superuser: it never crosses the tenant boundary, which is enforced separately by
the tenant key on every repository statement.
"""

from __future__ import annotations

from dataclasses import dataclass

# Every permission the API can require.
PERMISSIONS = (
    "manuscript.read",
    "manuscript.write",
    "audit.run",
    "audit.cancel",
    "review.read",
    "review.decide",
    "correction.propose",
    "correction.transition",
    "repair.run",
    "export.create",
    "export.read",
    "assignment.read",
    "assignment.write",
    "organization.read",
    "organization.write",
    "telemetry.read",
    "evaluation.read",
    "evaluation.run",
)

VIEWER = frozenset(
    {
        "manuscript.read",
        "review.read",
        "export.read",
        "assignment.read",
        "organization.read",
    }
)

REVIEWER = VIEWER | frozenset(
    {
        "review.decide",
        "correction.propose",
        "export.create",
        "repair.run",
        "telemetry.read",
        "evaluation.read",
    }
)

OPERATOR = VIEWER | frozenset(
    {
        "manuscript.write",
        "audit.run",
        "audit.cancel",
        "repair.run",
        "telemetry.read",
    }
)

# A senior reviewer accepts or rejects correction proposals and assigns work.
LEAD_REVIEWER = REVIEWER | frozenset(
    {"correction.transition", "assignment.write", "evaluation.run"}
)

ORG_ADMIN = LEAD_REVIEWER | OPERATOR | frozenset({"organization.write"})

ADMINISTRATOR = frozenset(PERMISSIONS)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": VIEWER,
    "reviewer": REVIEWER,
    "lead_reviewer": LEAD_REVIEWER,
    "operator": OPERATOR,
    "org_admin": ORG_ADMIN,
    "administrator": ADMINISTRATOR,
}

# Legacy names accepted from Phase 3 headers and from directory groups.
ROLE_ALIASES = {
    "admin": "administrator",
    "administrators": "administrator",
    "citeprism-admin": "administrator",
    "citeprism-operator": "operator",
    "citeprism-reviewer": "reviewer",
    "lead": "lead_reviewer",
    "senior_reviewer": "lead_reviewer",
    "read_only": "viewer",
    "readonly": "viewer",
}

KNOWN_ROLES = frozenset(ROLE_PERMISSIONS)


class AuthorizationError(PermissionError):
    """The principal is authenticated but lacks the required permission."""


def normalize_role(value: str) -> str | None:
    name = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    name = ROLE_ALIASES.get(name, ROLE_ALIASES.get(value.strip().lower(), name))
    return name if name in ROLE_PERMISSIONS else None


def normalize_roles(values) -> frozenset[str]:
    if isinstance(values, str):
        values = values.split(",")
    resolved = {normalize_role(item) for item in (values or [])}
    return frozenset(item for item in resolved if item)


def permissions_for(roles) -> frozenset[str]:
    granted: set[str] = set()
    for role in normalize_roles(roles):
        granted |= ROLE_PERMISSIONS[role]
    return frozenset(granted)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    permission: str
    roles: frozenset[str]

    @property
    def reason(self) -> str:
        if self.allowed:
            return "granted"
        held = ", ".join(sorted(self.roles)) or "none"
        return (
            f"the {self.permission} permission is required; "
            f"this account holds: {held}"
        )


def evaluate(roles, permission: str) -> AccessDecision:
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission: {permission}")
    resolved = normalize_roles(roles)
    return AccessDecision(permission in permissions_for(resolved), permission, resolved)


def require(roles, permission: str) -> None:
    decision = evaluate(roles, permission)
    if not decision.allowed:
        raise AuthorizationError(decision.reason)


def describe_roles() -> list[dict]:
    """Documentation payload served at /api/v1/organizations/roles."""
    return [
        {
            "role": role,
            "permissions": sorted(granted),
            "description": {
                "viewer": "Read manuscripts, review projections and exports.",
                "reviewer": "Record decisions, propose corrections and run repairs.",
                "lead_reviewer": "Reviewer plus correction acceptance and assignment.",
                "operator": "Upload manuscripts and run or cancel audits.",
                "org_admin": "Manage organization membership and all review work.",
                "administrator": "Every permission within this one tenant.",
            }[role],
        }
        for role, granted in ROLE_PERMISSIONS.items()
    ]


__all__ = [
    "AccessDecision",
    "AuthorizationError",
    "KNOWN_ROLES",
    "PERMISSIONS",
    "ROLE_PERMISSIONS",
    "describe_roles",
    "evaluate",
    "normalize_roles",
    "permissions_for",
    "require",
]

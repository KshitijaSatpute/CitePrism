"""OIDC bearer-token verification with a cached JWKS.

Phase 3 trusted an identity-aware reverse proxy. Phase 4 can verify tokens
itself: the API fetches the issuer's JWKS, caches it, and validates signature,
issuer, audience, expiry and not-before on every request.

Three properties are deliberate:

* **No unverified decode path.** There is no "decode without verification"
  helper anywhere in this module, so a future edit cannot accidentally trust an
  attacker-supplied token.
* **Algorithm allow-list.** `alg` comes from the token header, so it is checked
  against the configured list before use. `none` and HMAC algorithms are never
  accepted for an asymmetric issuer.
* **Bounded refresh.** An unknown `kid` triggers at most one JWKS refetch per
  cooldown window, so a token spraying random key identifiers cannot turn the
  API into a request amplifier against the identity provider.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ASYMMETRIC_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})
MIN_REFRESH_INTERVAL = 30.0


class OidcError(RuntimeError):
    """Token verification or provider discovery failed."""


@dataclass
class OidcSettings:
    issuer: str
    audience: str
    jwks_url: str | None = None
    algorithms: tuple[str, ...] = ("RS256",)
    tenant_claim: str = "org_id"
    roles_claim: str = "roles"
    subject_claim: str = "sub"
    email_claim: str = "email"
    name_claim: str = "name"
    cache_seconds: int = 600
    clock_skew_seconds: int = 60
    require_tenant_claim: bool = True
    role_map: dict[str, str] = field(default_factory=dict)

    def validated(self) -> "OidcSettings":
        if not self.issuer or urlparse(self.issuer).scheme not in {"https", "http"}:
            raise OidcError("the OIDC issuer must be an absolute URL")
        if urlparse(self.issuer).scheme == "http" and not _is_loopback(self.issuer):
            raise OidcError("a non-loopback OIDC issuer must use https")
        if not self.audience:
            raise OidcError("an OIDC audience is required")
        unsupported = set(self.algorithms) - ASYMMETRIC_ALGORITHMS
        if unsupported or not self.algorithms:
            raise OidcError("OIDC algorithms must be a non-empty asymmetric allow-list")
        return self


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


class JwksCache:
    """Thread-safe JWKS cache with bounded refresh on unknown key identifiers."""

    def __init__(self, settings: OidcSettings, *, fetch=None):
        self.settings = settings
        self._fetch = fetch or self._http_fetch
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._loaded_at = 0.0
        self._last_refresh_attempt = 0.0

    # -- discovery -----------------------------------------------------
    def jwks_url(self) -> str:
        if self.settings.jwks_url:
            return self.settings.jwks_url
        base = self.settings.issuer.rstrip("/")
        document = self._fetch(f"{base}/.well-known/openid-configuration")
        url = (document or {}).get("jwks_uri")
        if not url:
            raise OidcError("the issuer discovery document does not publish jwks_uri")
        if document.get("issuer") and document["issuer"].rstrip("/") != base:
            raise OidcError("the discovery document issuer does not match the configuration")
        self.settings.jwks_url = url
        return url

    @staticmethod
    def _http_fetch(url: str) -> dict:
        import json
        import urllib.request

        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            if response.status != 200:
                raise OidcError(f"identity provider returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))

    # -- key access ----------------------------------------------------
    def _load(self) -> None:
        document = self._fetch(self.jwks_url())
        keys = {}
        for entry in (document or {}).get("keys", []):
            identifier = entry.get("kid")
            if not identifier:
                continue
            keys[identifier] = entry
        if not keys:
            raise OidcError("the identity provider published no usable signing keys")
        self._keys = keys
        self._loaded_at = time.monotonic()

    def key_for(self, kid: str | None):
        now = time.monotonic()
        with self._lock:
            expired = now - self._loaded_at > max(30, self.settings.cache_seconds)
            if not self._keys or expired:
                self._load()
            if kid and kid not in self._keys:
                if now - self._last_refresh_attempt >= MIN_REFRESH_INTERVAL:
                    self._last_refresh_attempt = now
                    self._load()
            if not kid:
                if len(self._keys) != 1:
                    raise OidcError("the token header omits kid and the JWKS has several keys")
                return next(iter(self._keys.values()))
            key = self._keys.get(kid)
        if key is None:
            raise OidcError("the token signing key is not published by the issuer")
        return key


class OidcVerifier:
    """Verify a bearer token and project it onto a CitePrism principal."""

    def __init__(self, settings: OidcSettings, *, cache: JwksCache | None = None):
        self.settings = settings.validated()
        self.cache = cache or JwksCache(self.settings)

    @staticmethod
    def _library():
        try:
            import jwt
            from jwt import PyJWK
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise OidcError(
                "install pyjwt[crypto] to verify OIDC tokens, "
                "or run with CITEPRISM_AUTH_MODE=trusted_proxy"
            ) from exc
        return jwt, PyJWK

    def verify(self, token: str) -> dict:
        if not token or token.count(".") != 2:
            raise OidcError("a compact JWS bearer token is required")
        jwt, PyJWK = self._library()
        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:  # noqa: BLE001 - library-specific error types
            raise OidcError("the bearer token header could not be read") from exc
        algorithm = header.get("alg")
        if algorithm not in self.settings.algorithms:
            raise OidcError("the token algorithm is not permitted")
        key = PyJWK.from_dict(self.cache.key_for(header.get("kid"))).key
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self.settings.algorithms),
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=self.settings.clock_skew_seconds,
                options={
                    "require": ["exp", "iat", "iss", "aud"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise OidcError("the bearer token is not valid for this service") from exc
        return claims

    # -- claim projection ---------------------------------------------
    def roles(self, claims: dict) -> list[str]:
        raw = _claim(claims, self.settings.roles_claim)
        if isinstance(raw, str):
            raw = [item for item in raw.replace(",", " ").split() if item]
        values = list(raw or [])
        mapped = [self.settings.role_map.get(item, item) for item in values]
        return mapped

    def tenant(self, claims: dict) -> str | None:
        value = _claim(claims, self.settings.tenant_claim)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        return str(value) if value else None

    def subject(self, claims: dict) -> str:
        value = _claim(claims, self.settings.subject_claim) or claims.get("sub")
        if not value:
            raise OidcError("the bearer token has no subject claim")
        return str(value)

    def profile(self, claims: dict) -> dict:
        return {
            "subject": self.subject(claims),
            "email": _claim(claims, self.settings.email_claim),
            "display_name": _claim(claims, self.settings.name_claim),
        }


def _claim(claims: dict, path: str):
    """Read a claim, supporting dotted paths such as `realm_access.roles`."""
    current: Any = claims
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def build_verifier(config) -> OidcVerifier:
    settings = OidcSettings(
        issuer=config.OIDC_ISSUER or "",
        audience=config.OIDC_AUDIENCE or "",
        jwks_url=config.OIDC_JWKS_URL or None,
        algorithms=tuple(config.OIDC_ALGORITHMS),
        tenant_claim=config.OIDC_TENANT_CLAIM,
        roles_claim=config.OIDC_ROLES_CLAIM,
        subject_claim=config.OIDC_SUBJECT_CLAIM,
        email_claim=config.OIDC_EMAIL_CLAIM,
        name_claim=config.OIDC_NAME_CLAIM,
        cache_seconds=config.OIDC_JWKS_CACHE_SECONDS,
        clock_skew_seconds=config.OIDC_CLOCK_SKEW_SECONDS,
        require_tenant_claim=config.OIDC_REQUIRE_TENANT_CLAIM,
        role_map=config.oidc_role_map(),
    )
    return OidcVerifier(settings)


__all__ = ["JwksCache", "OidcError", "OidcSettings", "OidcVerifier", "build_verifier"]

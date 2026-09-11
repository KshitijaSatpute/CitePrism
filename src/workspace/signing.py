"""Detached signatures for review packages.

An exported review package leaves CitePrism and is read by an editor, an
integrity office or a court. The recipient needs to be able to answer two
questions without trusting whoever emailed them the file: is this the bytes
CitePrism produced, and which key vouched for it?

Ed25519 is the default because verification needs only the public key, which can
be published. HMAC is offered for deployments that cannot manage a key pair, but
it only proves the holder of a shared secret produced the file, so verification
requires that secret — a materially weaker claim, and the manifest says so.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SIGNATURE_VERSION = "citeprism-signature-v1"


class SigningError(RuntimeError):
    """The signing key is missing, unreadable or unusable."""


def canonical_manifest(manifest: dict) -> bytes:
    """Deterministic bytes for signing: sorted keys, no incidental whitespace."""
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True)
class Signature:
    algorithm: str
    key_id: str
    value: str
    public_key: str | None = None

    def as_dict(self) -> dict:
        return {
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "signature": self.value,
            "public_key": self.public_key,
            "signature_version": SIGNATURE_VERSION,
            "verification": (
                "Verify the manifest bytes against this signature, then confirm "
                "each listed file matches its recorded SHA-256."
                if self.algorithm == "ed25519"
                else "This is a shared-secret MAC. Verification requires the same "
                "secret, so it proves integrity to holders of that secret only."
            ),
        }


class Signer:
    """Sign manifests with Ed25519, HMAC-SHA256, or not at all."""

    def __init__(self, algorithm: str = "ed25519", *, key_path=None, key_id="citeprism-export-1",
                 hmac_secret: str | None = None):
        self.algorithm = (algorithm or "none").lower()
        self.key_id = key_id or "citeprism-export-1"
        self.key_path = Path(key_path) if key_path else None
        self.hmac_secret = hmac_secret
        self._private = None
        self._public_pem: str | None = None
        if self.algorithm not in {"ed25519", "hmac-sha256", "none"}:
            raise SigningError(f"unsupported signing algorithm: {self.algorithm}")

    @classmethod
    def from_config(cls, config) -> "Signer":
        return cls(
            algorithm=getattr(config, "EXPORT_SIGNING_ALGORITHM", "ed25519"),
            key_path=getattr(config, "EXPORT_SIGNING_KEY_PATH", None),
            key_id=getattr(config, "EXPORT_SIGNING_KEY_ID", "citeprism-export-1"),
            hmac_secret=getattr(config, "EXPORT_HMAC_SECRET", None),
        )

    # -- key handling --------------------------------------------------
    def _load_ed25519(self):
        if self._private is not None:
            return self._private
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise SigningError("install the cryptography package to sign exports") from exc

        if self.key_path and self.key_path.is_file():
            data = self.key_path.read_bytes()
            try:
                key = serialization.load_pem_private_key(data, password=None)
            except Exception as exc:  # noqa: BLE001
                raise SigningError("the export signing key could not be read") from exc
            if not isinstance(key, Ed25519PrivateKey):
                raise SigningError("the export signing key is not an Ed25519 private key")
        else:
            # An ephemeral key still proves the bytes were not altered in transit,
            # but it cannot prove which deployment produced them. The manifest
            # records this so nobody reads more into the signature than it means.
            logger.warning(
                "No export signing key is configured; generating an ephemeral key. "
                "Set CITEPRISM_EXPORT_SIGNING_KEY for verifiable, durable signatures."
            )
            key = Ed25519PrivateKey.generate()
            self.key_id = f"{self.key_id}-ephemeral"

        self._private = key
        self._public_pem = (
            key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )
        return key

    @staticmethod
    def generate_key(destination: str | Path) -> Path:
        """Write a new Ed25519 private key with owner-only permissions."""
        import os

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        destination.write_bytes(pem)
        try:
            os.chmod(destination, 0o600)
        except OSError:  # pragma: no cover - Windows ACLs differ
            logger.info("Could not set POSIX permissions on the signing key.")
        public = destination.with_suffix(".pub")
        public.write_bytes(
            key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        return destination

    def public_key_pem(self) -> str | None:
        if self.algorithm != "ed25519":
            return None
        self._load_ed25519()
        return self._public_pem

    # -- signing -------------------------------------------------------
    def sign(self, payload: bytes) -> Signature:
        if self.algorithm == "none":
            return Signature("none", self.key_id, "", None)
        if self.algorithm == "hmac-sha256":
            if not self.hmac_secret:
                raise SigningError("an HMAC secret is required for hmac-sha256 signing")
            digest = hmac.new(
                self.hmac_secret.encode("utf-8"), payload, hashlib.sha256
            ).digest()
            return Signature("hmac-sha256", self.key_id, base64.b64encode(digest).decode("ascii"))
        key = self._load_ed25519()
        signature = key.sign(payload)
        return Signature(
            "ed25519",
            self.key_id,
            base64.b64encode(signature).decode("ascii"),
            self._public_pem,
        )

    def sign_manifest(self, manifest: dict) -> Signature:
        return self.sign(canonical_manifest(manifest))


def verify_manifest(
    manifest: dict,
    signature: str,
    *,
    algorithm: str,
    public_key_pem: str | None = None,
    hmac_secret: str | None = None,
) -> bool:
    """Verify a detached signature over a manifest. Used by tests and auditors."""
    payload = canonical_manifest(manifest)
    raw = base64.b64decode(signature.encode("ascii"))
    if algorithm == "hmac-sha256":
        if not hmac_secret:
            raise SigningError("verification of an HMAC signature requires the shared secret")
        expected = hmac.new(hmac_secret.encode("utf-8"), payload, hashlib.sha256).digest()
        return hmac.compare_digest(expected, raw)
    if algorithm != "ed25519":
        return False
    if not public_key_pem:
        raise SigningError("verification of an Ed25519 signature requires the public key")
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    try:
        key.verify(raw, payload)
        return True
    except Exception:  # noqa: BLE001 - any failure is a failed verification
        return False


__all__ = [
    "SIGNATURE_VERSION",
    "Signature",
    "Signer",
    "SigningError",
    "canonical_manifest",
    "verify_manifest",
]

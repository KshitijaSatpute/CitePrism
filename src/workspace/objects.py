"""Workspace object storage: content-addressed local or S3-compatible backends.

The Phase 1 trust object store is write-only by design — it publishes immutable
evidence and never reads it back. The reviewer workspace needs more: it streams
manuscript PDFs to the browser, reads evidence artifacts inside the worker and
hands signed export packages to auditors.

This module therefore adds a read side, keeps the same content addressing, and
supports any S3-compatible endpoint (AWS S3, MinIO, Ceph RGW) so the compose
preview and a production deployment use one code path.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from src.trust.ports import TrustConfigurationError, TrustIntegrityError
from src.utils.safe_io import sha256_file

logger = logging.getLogger(__name__)

SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_PREFIX = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
CHUNK = 1024 * 1024


def safe_component(value: str) -> bool:
    """A path component that cannot traverse.

    The character class alone is not enough: '.' is a permitted character, so
    '..' matches it. Relative components must be rejected explicitly.
    """
    if not value or not SAFE_PREFIX.fullmatch(value):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


@dataclass(frozen=True)
class ObjectRef:
    """Where an artifact lives and what it is, independent of the backend."""

    backend: str
    key: str
    content_sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    version_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "key": self.key,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "version_id": self.version_id,
        }


def content_key(namespace: str, category: str, digest: str, *, suffix: str = "") -> str:
    """Build a stable, traversal-free key: <tenant>/<category>/sha256/ab/cd/<digest><suffix>."""
    if not SHA256.fullmatch(digest):
        raise ValueError("object keys require a lowercase SHA-256 digest")
    if not safe_component(namespace) or not safe_component(category):
        raise ValueError("object namespace and category must be safe path components")
    if suffix and not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        raise ValueError("object suffix must be a short alphanumeric extension")
    return f"{namespace}/{category}/sha256/{digest[:2]}/{digest[2:4]}/{digest}{suffix}"


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class LocalWorkspaceObjectStore:
    """Content-addressed files under one canonical root.

    Symlinks and Windows reparse points are rejected on every access so a
    tampered directory cannot redirect a read or write outside the root.
    """

    backend = "local"

    def __init__(self, root: str | Path):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        self._root_stat = self.root.stat()

    @staticmethod
    def _checked(path: Path, *, directory: bool = False):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        reparse = getattr(info, "st_file_attributes", 0) & 0x400
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise TrustIntegrityError("managed object path contains a symlink or reparse point")
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not expected:
            raise TrustIntegrityError("managed object path has an unexpected entry type")
        return info

    def _resolve(self, key: str, *, create: bool = False) -> Path:
        if key.startswith("/") or ".." in key.split("/") or "\\" in key:
            raise ValueError("unsafe object key")
        root_info = self._checked(self.root, directory=True)
        if root_info is None or not os.path.samestat(root_info, self._root_stat):
            raise TrustIntegrityError("configured object root was removed or replaced")
        parent, parts = self.root, key.split("/")
        for component in parts[:-1]:
            parent = parent / component
            if self._checked(parent, directory=True) is None:
                if not create:
                    return parent / parts[-1]
                parent.mkdir(exist_ok=True)
                if self._checked(parent, directory=True) is None:
                    raise TrustIntegrityError("managed object directory disappeared")
        return parent / parts[-1]

    def put_file(
        self, path: str | Path, *, key: str, content_sha256: str, media_type: str
    ) -> ObjectRef:
        path = Path(path)
        if sha256_file(path) != content_sha256:
            raise TrustIntegrityError("source digest mismatch")
        destination = self._resolve(key, create=True)
        if self._checked(destination) is None:
            handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".incoming-")
            try:
                with os.fdopen(handle, "wb") as target, path.open("rb") as source:
                    shutil.copyfileobj(source, target, CHUNK)
                    target.flush()
                    os.fsync(target.fileno())
                if sha256_file(Path(temporary)) != content_sha256:
                    raise TrustIntegrityError("source changed during object write")
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    pass
                except OSError:
                    # Filesystems without hard links (some Windows shares).
                    os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        info = self._checked(destination)
        if info is None or sha256_file(destination) != content_sha256:
            raise TrustIntegrityError("stored object digest mismatch")
        return ObjectRef(self.backend, key, content_sha256, info.st_size, media_type)

    def put_bytes(self, payload: bytes, *, key: str, media_type: str) -> ObjectRef:
        digest = digest_bytes(payload)
        with tempfile.TemporaryDirectory(prefix="citeprism-object-") as directory:
            staged = Path(directory) / "payload"
            staged.write_bytes(payload)
            return self.put_file(
                staged, key=key, content_sha256=digest, media_type=media_type
            )

    def get_bytes(self, key: str, *, expected_sha256: str | None = None) -> bytes:
        destination = self._resolve(key)
        if self._checked(destination) is None:
            raise FileNotFoundError(f"object {key} is not stored")
        payload = destination.read_bytes()
        if expected_sha256 and digest_bytes(payload) != expected_sha256:
            raise TrustIntegrityError("stored object digest mismatch on read")
        return payload

    def open_stream(self, key: str) -> BinaryIO:
        destination = self._resolve(key)
        if self._checked(destination) is None:
            raise FileNotFoundError(f"object {key} is not stored")
        return destination.open("rb")

    def iter_chunks(self, key: str) -> Iterator[bytes]:
        with self.open_stream(key) as handle:
            while chunk := handle.read(CHUNK):
                yield chunk

    def exists(self, key: str) -> bool:
        try:
            return self._checked(self._resolve(key)) is not None
        except (ValueError, TrustIntegrityError):
            return False

    def local_path(self, key: str) -> Path | None:
        destination = self._resolve(key)
        return destination if self._checked(destination) is not None else None

    def presigned_url(self, key: str, *, expires_in: int = 300) -> str | None:
        """Local storage has no signed URL; callers stream through the API."""
        del key, expires_in
        return None


class S3WorkspaceObjectStore:
    """Any S3-compatible endpoint. Path-style addressing keeps MinIO working."""

    backend = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "citeprism/workspace",
        client=None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        addressing: str = "path",
        server_side_encryption: str | None = "AES256",
        kms_key_id: str | None = None,
        require_versioning: bool = False,
        signed_url_seconds: int = 300,
    ):
        if not bucket:
            raise TrustConfigurationError("an S3 bucket name is required")
        if server_side_encryption and server_side_encryption not in {
            "AES256",
            "aws:kms",
            "aws:kms:dsse",
        }:
            raise TrustConfigurationError("invalid S3 encryption algorithm")
        if kms_key_id and server_side_encryption == "AES256":
            raise TrustConfigurationError("a KMS key requires KMS encryption")
        if client is None:
            try:
                import boto3
                from botocore.config import Config as BotoConfig
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise TrustConfigurationError("install boto3 for S3 object storage") from exc
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region_name,
                config=BotoConfig(
                    s3={"addressing_style": "path" if addressing == "path" else "virtual"},
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.encryption = server_side_encryption
        self.kms_key = kms_key_id
        self.require_versioning = require_versioning
        self.signed_url_seconds = max(30, int(signed_url_seconds))
        if require_versioning:
            status = client.get_bucket_versioning(Bucket=bucket).get("Status")
            if status != "Enabled":
                raise TrustConfigurationError("S3 bucket versioning must be enabled")

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _head(self, key: str, version_id: str | None = None):
        arguments = {"Bucket": self.bucket, "Key": self._full(key)}
        if version_id:
            arguments["VersionId"] = version_id
        try:
            return self.client.head_object(**arguments)
        except Exception as error:  # noqa: BLE001 - boto raises client-specific types
            code = str(getattr(error, "response", {}).get("Error", {}).get("Code"))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def _encryption_arguments(self) -> dict:
        if not self.encryption:
            return {}
        arguments = {"ServerSideEncryption": self.encryption}
        if self.kms_key:
            arguments["SSEKMSKeyId"] = self.kms_key
        return arguments

    def put_file(
        self, path: str | Path, *, key: str, content_sha256: str, media_type: str
    ) -> ObjectRef:
        path = Path(path)
        if sha256_file(path) != content_sha256:
            raise TrustIntegrityError("source digest mismatch")
        return self._put(path.open("rb"), path.stat().st_size, key, content_sha256, media_type)

    def put_bytes(self, payload: bytes, *, key: str, media_type: str) -> ObjectRef:
        digest = digest_bytes(payload)
        return self._put(io.BytesIO(payload), len(payload), key, digest, media_type)

    def _put(self, body, size: int, key: str, digest: str, media_type: str) -> ObjectRef:
        head = self._head(key)
        if head is None:
            arguments = {
                "Bucket": self.bucket,
                "Key": self._full(key),
                "ContentType": media_type,
                "ContentLength": size,
                "Metadata": {"content-sha256": digest},
                **self._encryption_arguments(),
            }
            try:
                with body:
                    result = self.client.put_object(Body=body, **arguments)
                head = self._head(key, result.get("VersionId")) or self._head(key)
            except Exception as error:  # noqa: BLE001
                code = str(getattr(error, "response", {}).get("Error", {}).get("Code"))
                if code not in {"412", "PreconditionFailed"}:
                    raise
                head = self._head(key)
        if not head:
            raise TrustIntegrityError("the S3 write could not be verified")
        version = head.get("VersionId")
        if self.require_versioning and (not version or version == "null"):
            raise TrustIntegrityError("S3 did not supply an immutable version identifier")
        if int(head.get("ContentLength", -1)) != size:
            raise TrustIntegrityError("S3 object size mismatch")
        if (head.get("Metadata") or {}).get("content-sha256") != digest:
            raise TrustIntegrityError("S3 metadata digest mismatch")
        return ObjectRef(self.backend, key, digest, size, media_type, version)

    def get_bytes(self, key: str, *, expected_sha256: str | None = None) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=self._full(key))
        stream = response["Body"]
        try:
            payload = stream.read()
        finally:
            stream.close()
        if expected_sha256 and digest_bytes(payload) != expected_sha256:
            raise TrustIntegrityError("stored object digest mismatch on read")
        return payload

    def open_stream(self, key: str) -> BinaryIO:
        return self.client.get_object(Bucket=self.bucket, Key=self._full(key))["Body"]

    def iter_chunks(self, key: str) -> Iterator[bytes]:
        stream = self.open_stream(key)
        try:
            while chunk := stream.read(CHUNK):
                yield chunk
        finally:
            stream.close()

    def exists(self, key: str) -> bool:
        return self._head(key) is not None

    def local_path(self, key: str) -> Path | None:
        del key
        return None

    def presigned_url(self, key: str, *, expires_in: int | None = None) -> str | None:
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": self._full(key)},
                ExpiresIn=int(expires_in or self.signed_url_seconds),
            )
        except Exception:  # noqa: BLE001 - signing is a convenience, never required
            logger.warning("Could not create a presigned URL; the API will stream the object.")
            return None


def build_object_store(config):
    """Create the configured workspace object store."""
    backend = getattr(config, "WORKSPACE_OBJECT_BACKEND", "local")
    if backend == "local":
        root = Path(getattr(config, "WORKSPACE_OBJECT_DIR", "data/workspace_objects"))
        if not root.is_absolute():
            root = Path(getattr(config, "BASE_DIR", ".")) / root
        return LocalWorkspaceObjectStore(root)
    return S3WorkspaceObjectStore(
        bucket=config.WORKSPACE_S3_BUCKET,
        prefix=config.WORKSPACE_S3_PREFIX,
        endpoint_url=config.WORKSPACE_S3_ENDPOINT_URL or None,
        region_name=config.WORKSPACE_S3_REGION or None,
        addressing=config.WORKSPACE_S3_ADDRESSING,
        server_side_encryption=config.WORKSPACE_S3_SERVER_SIDE_ENCRYPTION or None,
        kms_key_id=config.WORKSPACE_S3_KMS_KEY_ID or None,
        require_versioning=config.WORKSPACE_S3_REQUIRE_VERSIONING,
        signed_url_seconds=config.WORKSPACE_S3_SIGNED_URL_SECONDS,
    )


__all__ = [
    "LocalWorkspaceObjectStore",
    "ObjectRef",
    "S3WorkspaceObjectStore",
    "build_object_store",
    "content_key",
    "digest_bytes",
    "safe_component",
]

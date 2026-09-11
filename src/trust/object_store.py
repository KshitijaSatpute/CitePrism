"""Content-addressed local and S3 storage; writes never replace existing keys."""
import base64
import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from src.trust.models import StoredObject
from src.trust.ports import TrustConfigurationError, TrustIntegrityError
from src.utils.safe_io import sha256_file


def object_key_for_digest(digest, prefix=""):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid SHA-256")
    if prefix and not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
        raise ValueError("prefix must use safe relative path components")
    return f"{prefix + '/' if prefix else ''}sha256/{digest[:2]}/{digest[2:4]}/{digest}"


class LocalObjectStore:
    def __init__(self, root, *, namespace=""):
        # Canonicalize an existing directory, never a partially created object
        # path. Windows realpath can use different path spellings as entries
        # appear; lexical comparisons of those spellings are not containment.
        object_key_for_digest("0" * 64, namespace)
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        self._root_stat = self.root.stat()
        self.namespace = namespace

    @staticmethod
    def _checked_entry(path, *, directory=False):
        """Inspect the entry itself; do not follow a symlink or Windows junction."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        reparse = getattr(info, "st_file_attributes", 0) & 0x400
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise TrustIntegrityError("managed object path contains a symlink or reparse point")
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise TrustIntegrityError("managed object path has an unexpected entry type")
        return info

    def _destination(self, key):
        # key comes exclusively from object_key_for_digest: no absolute paths,
        # empty components, backslashes, '.' or '..'. Walk existing directories
        # and reject redirection before creating anything beneath them. Keep the
        # digest filename unchanged; resolving hard-link names is unnecessary.
        root_info = self._checked_entry(self.root, directory=True)
        if root_info is None or not os.path.samestat(root_info, self._root_stat):
            raise TrustIntegrityError("configured object root was removed or replaced")
        parent = self.root
        components = key.split("/")
        for component in components[:-1]:
            parent = parent / component
            if self._checked_entry(parent, directory=True) is None:
                try:
                    parent.mkdir()
                except FileExistsError:
                    pass  # Another writer created the same directory.
                if self._checked_entry(parent, directory=True) is None:
                    raise TrustIntegrityError("managed object directory disappeared during creation")
        return parent / components[-1]

    def put_file(self, path, *, content_sha256, media_type):
        del media_type
        path = Path(path)
        key = object_key_for_digest(content_sha256, self.namespace)
        if sha256_file(path) != content_sha256:
            raise TrustIntegrityError("source digest mismatch")
        destination = self._destination(key)
        if self._checked_entry(destination) is None:
            fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".incoming-")
            try:
                with os.fdopen(fd, "wb") as target, path.open("rb") as source:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                if sha256_file(Path(name)) != content_sha256:
                    raise TrustIntegrityError("source changed during object write")
                try:
                    # Atomic create-only publication; concurrent writers never overwrite.
                    os.link(name, destination)
                except FileExistsError:
                    pass
            finally:
                Path(name).unlink(missing_ok=True)
        stored_info = self._checked_entry(destination)
        if stored_info is None or sha256_file(destination) != content_sha256:
            raise TrustIntegrityError("stored object digest mismatch")
        return StoredObject(backend="local", key=key, content_sha256=content_sha256,
                            size_bytes=stored_info.st_size)


class S3ObjectStore:
    def __init__(self, *, bucket, prefix="citeprism", client=None,
                 endpoint_url=None, region_name=None, server_side_encryption="AES256",
                 kms_key_id=None, require_versioning=True):
        if not bucket:
            raise TrustConfigurationError("S3 bucket is required")
        if server_side_encryption not in {"AES256", "aws:kms", "aws:kms:dsse"}:
            raise TrustConfigurationError("invalid S3 encryption algorithm")
        if kms_key_id and server_side_encryption == "AES256":
            raise TrustConfigurationError("KMS key requires KMS encryption")
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise TrustConfigurationError("install boto3 for S3 storage") from exc
            client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region_name)
        self.client, self.bucket, self.prefix = client, bucket, prefix
        self.encryption, self.kms_key = server_side_encryption, kms_key_id
        self.require_versioning = require_versioning
        if require_versioning and client.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
            raise TrustConfigurationError("S3 bucket versioning must be enabled")

    def _head(self, key, version_id=None):
        args = {"Bucket": self.bucket, "Key": key, "ChecksumMode": "ENABLED"}
        if version_id:
            args["VersionId"] = version_id
        try:
            return self.client.head_object(**args)
        except Exception as exc:
            response = getattr(exc, "response", {})
            if str(response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def put_file(self, path, *, content_sha256, media_type):
        path = Path(path)
        if sha256_file(path) != content_sha256:
            raise TrustIntegrityError("source digest mismatch")
        key = object_key_for_digest(content_sha256, self.prefix)
        checksum = base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")
        head = self._head(key)
        if head is None:
            args = dict(Bucket=self.bucket, Key=key, IfNoneMatch="*", ContentType=media_type,
                        ContentLength=path.stat().st_size, ChecksumSHA256=checksum,
                        Metadata={"content-sha256": content_sha256}, ServerSideEncryption=self.encryption)
            if self.kms_key:
                args["SSEKMSKeyId"] = self.kms_key
            try:
                with path.open("rb") as body:
                    result = self.client.put_object(Body=body, **args)
                head = self._head(key, result.get("VersionId"))
            except Exception as exc:
                response = getattr(exc, "response", {})
                if str(response.get("Error", {}).get("Code")) not in {"412", "PreconditionFailed"}:
                    raise
                head = self._head(key)
        if not head:
            raise TrustIntegrityError("S3 write could not be verified")
        version = head.get("VersionId")
        if self.require_versioning and (not version or version == "null"):
            raise TrustIntegrityError("S3 did not supply an immutable version identifier")
        if head.get("ServerSideEncryption") != self.encryption:
            raise TrustIntegrityError("S3 object encryption does not match policy")
        if head.get("ContentLength") != path.stat().st_size:
            raise TrustIntegrityError("S3 object size mismatch")
        if (head.get("Metadata") or {}).get("content-sha256") != content_sha256:
            raise TrustIntegrityError("S3 metadata digest mismatch")
        if head.get("ChecksumSHA256"):
            if head["ChecksumSHA256"] != checksum:
                raise TrustIntegrityError("S3 checksum mismatch")
        else:
            args = {"Bucket": self.bucket, "Key": key}
            if version:
                args["VersionId"] = version
            body = self.client.get_object(**args)["Body"]
            digest = hashlib.sha256()
            try:
                while chunk := body.read(1024 * 1024):
                    digest.update(chunk)
            finally:
                body.close()
            if digest.hexdigest() != content_sha256:
                raise TrustIntegrityError("S3 content digest mismatch")
        return StoredObject(backend="s3", key=key, content_sha256=content_sha256,
                            size_bytes=head["ContentLength"], version_id=version)

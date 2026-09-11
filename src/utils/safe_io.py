"""Safe local I/O primitives used by the Phase 0 pipeline.

The current application is still a single-user prototype. These helpers do not
replace a transactional object store, but they prevent partial JSON artifacts
and filename collisions while the V3 trust kernel is built.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Type


PDF_MAGIC = b"%PDF-"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of *path* without loading it into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_pdf(path: Path, *, max_bytes: int) -> None:
    """Reject empty, oversized, or obviously non-PDF uploads."""
    path = Path(path)
    size = path.stat().st_size
    if size == 0:
        raise ValueError("The uploaded PDF is empty.")
    if size > max_bytes:
        raise ValueError(
            f"The uploaded PDF is {size / (1024 * 1024):.1f} MiB; "
            f"the configured limit is {max_bytes / (1024 * 1024):.1f} MiB."
        )
    with path.open("rb") as handle:
        if handle.read(len(PDF_MAGIC)) != PDF_MAGIC:
            raise ValueError("The upload does not have a valid PDF file signature.")


def safe_stem(filename: str, fallback: str = "manuscript") -> str:
    """Produce a conservative display-derived filename component."""
    stem = Path(filename).stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-_")
    return cleaned[:80] or fallback


def content_addressed_pdf_name(original_name: str, digest: str) -> str:
    """Return a canonical storage name; the original name is metadata only."""
    del original_name  # Retained in the signature for call-site compatibility.
    return f"{digest}.pdf"


def atomic_write_json(
    destination: Path,
    payload: Any,
    *,
    encoder: Type[json.JSONEncoder] | None = None,
) -> None:
    """Atomically replace a JSON file after a complete write and fsync."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                indent=2,
                ensure_ascii=False,
                cls=encoder,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_copy_file(source: Path, destination: Path) -> None:
    """Copy a file into place atomically on the destination filesystem."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    try:
        with source.open("rb") as source_handle, os.fdopen(fd, "wb") as temp_handle:
            shutil.copyfileobj(source_handle, temp_handle)
            temp_handle.flush()
            os.fsync(temp_handle.fileno())
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise

"""Primitives shared by every workspace backend.

These were defined inside the Phase 3 SQLite store. Phase 4 adds a second
backend, so they live here and the SQLite module re-exports them unchanged for
backwards compatibility with existing imports and tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class WorkspaceNotFound(LookupError):
    """A tenant-scoped identifier did not resolve. Never leaks cross-tenant rows."""


class WorkspaceConflict(RuntimeError):
    """A concurrent or out-of-order write was rejected."""


def utc_now() -> str:
    """RFC 3339 UTC with a trailing Z, stable across backends and hash chains."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encode(value: Any) -> str:
    """Canonical JSON: sorted keys, no spaces, no NaN. Used for hashing."""
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    )


def decode(value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


__all__ = ["WorkspaceConflict", "WorkspaceNotFound", "decode", "encode", "utc_now"]

"""Version-1 canonical JSON serialization (not an RFC 8785 implementation)."""
import hashlib
import json
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel


def _default(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (UUID, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value) -> str:
    def normalize(item):
        if isinstance(item, BaseModel):
            return normalize(item.model_dump(mode="python"))
        if isinstance(item, dict):
            return {key: normalize(part) for key, part in item.items()}
        if isinstance(item, (tuple, list)):
            return [normalize(part) for part in item]
        if isinstance(item, float) and item == 0:
            return 0.0  # PostgreSQL JSONB does not retain the sign of negative zero.
        if isinstance(item, datetime):
            # Match Pydantic JSON-mode encoding of aware UTC datetimes.
            result = item.isoformat()
            return result[:-6] + "Z" if result.endswith("+00:00") else result
        return item

    return json.dumps(normalize(value), default=_default, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":"))


def canonical_object(value):
    return json.loads(canonical_json(value))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value) -> str:
    return sha256_text(canonical_json(value))

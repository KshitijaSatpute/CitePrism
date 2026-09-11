"""UUIDv7 creation and collision-safe deterministic import identifiers."""
import json
import secrets
from datetime import datetime, timezone
from uuid import UUID, uuid5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid7(now: datetime | None = None) -> UUID:
    now = now or utc_now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("UUIDv7 requires timezone-aware time")
    milliseconds = int(now.timestamp() * 1000)
    if not 0 <= milliseconds < 2**48:
        raise ValueError("timestamp outside UUIDv7 range")
    random = secrets.randbits(74)
    return UUID(int=(milliseconds << 80) | (7 << 76) | ((random >> 62) << 64)
                | (2 << 62) | (random & (2**62 - 1)))


def derived_uuid(namespace: UUID, *parts: object) -> UUID:
    # Length/structure is preserved; ('a:b', 'c') must differ from ('a', 'b:c').
    return uuid5(namespace, json.dumps([str(p) for p in parts], separators=(",", ":")))

"""Process-wide, per-provider request pacing.

Concurrent enrichment is only safe if it stays polite. Scholarly indexes publish
rate limits, and exceeding them gets an IP throttled for everyone behind it —
including the reviewer's whole university network. Every outbound metadata call
therefore takes a token from the bucket for its provider before it is sent.

The buckets are shared across threads in one process. Separate worker processes
each hold their own buckets, so the configured rates are per process; size the
worker fleet with that in mind.
"""

from __future__ import annotations

import os
import threading
import time

# Requests per second. Conservative defaults below the published ceilings:
#   OpenAlex polite pool 10/s; Crossref polite pool is generous but asks for
#   restraint; Semantic Scholar's unauthenticated pool is shared and tight;
#   Unpaywall asks for <= 100k/day.
DEFAULT_RATES = {
    "openalex": 8.0,
    "crossref": 8.0,
    "semantic_scholar": 0.9,
    "semantic_scholar_keyed": 0.9,
    "unpaywall": 5.0,
    "europe_pmc": 5.0,
    "dblp": 1.0,
    "datacite": 5.0,
    "doi_org": 5.0,
    "full_text_host": 1.0,
}


class TokenBucket:
    """A thread-safe token bucket. `acquire` blocks until a token is free."""

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = max(0.01, float(rate))
        self.capacity = max(1.0, float(burst if burst is not None else min(self.rate, 5.0)))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = 120.0) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self.rate
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 1.0))


_buckets: dict[str, TokenBucket] = {}
_registry_lock = threading.Lock()


def _configured_rate(provider: str) -> float:
    variable = f"CITEPRISM_RATE_{provider.upper()}"
    raw = os.getenv(variable)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_RATES.get(provider, 2.0)


def bucket(provider: str) -> TokenBucket:
    with _registry_lock:
        existing = _buckets.get(provider)
        if existing is None:
            existing = TokenBucket(_configured_rate(provider))
            _buckets[provider] = existing
        return existing


def acquire(provider: str, timeout: float | None = 120.0) -> None:
    """Block until `provider` may be called. Raises TimeoutError on starvation."""
    if not bucket(provider).acquire(timeout=timeout):
        raise TimeoutError(f"rate limiter starved waiting for {provider}")


def reset() -> None:
    """Testing hook: drop every bucket so new rates take effect."""
    with _registry_lock:
        _buckets.clear()


__all__ = ["DEFAULT_RATES", "TokenBucket", "acquire", "bucket", "reset"]

"""Collect stage timings and provider usage, then persist them in one write.

Telemetry is best effort. A failure to record a measurement never fails an
audit, and no prompt text, response body or credential ever reaches this module
— only stage names, durations, token counts and outcome codes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from src.telemetry.pricing import PricingTable

logger = logging.getLogger(__name__)

# Provider usage dictionaries differ by SDK; these are the observed key names.
INPUT_TOKEN_KEYS = (
    "prompt_token_count",
    "input_tokens",
    "prompt_tokens",
    "promptTokenCount",
)
OUTPUT_TOKEN_KEYS = (
    "candidates_token_count",
    "output_tokens",
    "completion_tokens",
    "candidatesTokenCount",
)


def _tokens(usage: Any, keys: tuple[str, ...]) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return 0


@dataclass
class TelemetryEvent:
    stage: str
    duration_ms: int = 0
    provider: str | None = None
    model: str | None = None
    outcome: str = "succeeded"
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    cost_usd: float = 0.0
    audit_id: str | None = None
    job_id: str | None = None
    attributes: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "duration_ms": self.duration_ms,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "requests": self.requests,
            "cost_usd": self.cost_usd,
            "audit_id": self.audit_id,
            "job_id": self.job_id,
            "attributes": self.attributes,
        }


class StageTimer:
    """Context manager that records one stage duration and its outcome."""

    def __init__(self, recorder: "TelemetryRecorder", stage: str, **attributes):
        self.recorder = recorder
        self.stage = stage
        self.attributes = attributes
        self.started = 0.0

    def __enter__(self) -> "StageTimer":
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        elapsed = int((time.perf_counter() - self.started) * 1000)
        outcome = "succeeded" if exc_type is None else "failed"
        attributes = dict(self.attributes)
        if exc is not None:
            attributes["error_code"] = str(
                getattr(exc, "code", None) or type(exc).__name__
            )[:100]
        self.recorder.add(
            TelemetryEvent(
                stage=self.stage,
                duration_ms=elapsed,
                outcome=outcome,
                attributes=attributes,
            )
        )
        return False  # never swallow the exception


class TelemetryRecorder:
    """Buffer telemetry for one audit and flush it to the workspace store."""

    def __init__(
        self,
        store=None,
        *,
        tenant_id: str | None = None,
        audit_id: str | None = None,
        job_id: str | None = None,
        pricing: PricingTable | None = None,
        enabled: bool = True,
    ):
        self.store = store
        self.tenant_id = tenant_id
        self.audit_id = audit_id
        self.job_id = job_id
        self.pricing = pricing or PricingTable()
        self.enabled = enabled
        self.events: list[TelemetryEvent] = []

    def stage(self, name: str, **attributes) -> StageTimer:
        return StageTimer(self, name, **attributes)

    def add(self, event: TelemetryEvent) -> None:
        event.audit_id = event.audit_id or self.audit_id
        event.job_id = event.job_id or self.job_id
        self.events.append(event)

    def record_stage(self, stage: str, duration_seconds: float, **attributes) -> None:
        self.add(
            TelemetryEvent(
                stage=stage,
                duration_ms=int(max(0.0, float(duration_seconds)) * 1000),
                attributes=attributes,
            )
        )

    def record_provider_calls(self, calls: list[dict]) -> int:
        """Turn recorded provider calls into billable telemetry.

        The Phase 2 `CallRecorder` already keeps an exact per-call record. This
        reads only its non-sensitive fields.
        """
        added = 0
        for call in calls or []:
            if not isinstance(call, dict):
                continue
            usage = call.get("usage") or {}
            inbound = _tokens(usage, INPUT_TOKEN_KEYS)
            outbound = _tokens(usage, OUTPUT_TOKEN_KEYS)
            provider = call.get("provider")
            model = call.get("model")
            event = TelemetryEvent(
                stage=str(call.get("purpose") or "provider_call")[:100],
                duration_ms=int(float(call.get("elapsed_seconds") or 0.0) * 1000),
                provider=provider,
                model=model,
                outcome=str(call.get("status") or "succeeded"),
                input_tokens=inbound,
                output_tokens=outbound,
                requests=max(1, len(call.get("attempts") or [1])),
                attributes={
                    "prompt_version": call.get("prompt_version"),
                    "error_code": call.get("error_code"),
                },
            )
            event.cost_usd = self.pricing.cost(
                provider=provider,
                model=model,
                input_tokens=inbound,
                output_tokens=outbound,
                requests=event.requests,
            )
            self.add(event)
            added += 1
        return added

    def totals(self) -> dict:
        return {
            "events": len(self.events),
            "duration_ms": sum(event.duration_ms for event in self.events),
            "input_tokens": sum(event.input_tokens for event in self.events),
            "output_tokens": sum(event.output_tokens for event in self.events),
            "requests": sum(event.requests for event in self.events),
            "cost_usd": round(sum(event.cost_usd for event in self.events), 6),
            "pricing_table_version": self.pricing.version,
        }

    def flush(self) -> int:
        if not self.enabled or self.store is None or not self.tenant_id or not self.events:
            self.events.clear()
            return 0
        payload = [event.as_dict() for event in self.events]
        try:
            written = self.store.record_telemetry(self.tenant_id, payload)
        except Exception:  # noqa: BLE001 - telemetry must never fail an audit
            logger.exception("Telemetry could not be persisted; the audit is unaffected.")
            written = 0
        self.events.clear()
        return written


__all__ = ["StageTimer", "TelemetryEvent", "TelemetryRecorder"]

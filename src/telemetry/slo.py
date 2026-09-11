"""Service level objectives computed from the telemetry and job history.

Each objective states a target, the measured value, and whether the target was
met over the window. Nothing here infers a cause: a missed objective is a
prompt to investigate, and the payload carries the sample size so a single slow
audit in a quiet week is not read as a regression.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value at or above the given rank.

    Returns 0.0 for an empty sample so a quiet window reads as "no data" rather
    than as a breach.
    """
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return 0.0
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return round(ordered[rank - 1], 3)


@dataclass
class SloResult:
    key: str
    title: str
    target: float
    measured: float
    unit: str
    comparison: str  # "at_most" or "at_least"
    sample_size: int
    met: bool
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SloReport:
    window_hours: int
    generated_at: str
    results: list[SloResult]
    totals: dict

    def as_dict(self) -> dict:
        return {
            "window_hours": self.window_hours,
            "generated_at": self.generated_at,
            "objectives": [result.as_dict() for result in self.results],
            "totals": self.totals,
            "met": all(result.met for result in self.results),
            "note": (
                "Objectives describe this deployment's observed behavior. "
                "They are not a warranty and not an authorization to operate."
            ),
        }


def _result(key, title, target, measured, unit, comparison, sample, detail) -> SloResult:
    if sample == 0:
        met = True
        detail = f"{detail} No samples in this window."
    elif comparison == "at_most":
        met = measured <= target
    else:
        met = measured >= target
    return SloResult(key, title, target, measured, unit, comparison, sample, met, detail)


def evaluate_slos(config, *, jobs: list[dict], telemetry: list[dict], queue: dict) -> SloReport:
    """Build the objective report from completed jobs and recorded telemetry."""
    from src.workspace.common import utc_now

    window = int(getattr(config, "SLO_WINDOW_HOURS", 168))
    finished = [job for job in jobs if job.get("state") in {"succeeded", "failed", "cancelled"}]
    succeeded = [job for job in finished if job.get("state") == "succeeded"]

    run_times = [float(job.get("run_seconds") or 0.0) for job in succeeded]
    queue_waits = [float(job.get("queue_wait_seconds") or 0.0) for job in finished]

    success_ratio = (len(succeeded) / len(finished)) if finished else 0.0

    resolved = sum(int((job.get("summary") or {}).get("resolved_references", 0)) for job in succeeded)
    total_refs = sum(
        int((job.get("summary") or {}).get("total_references", 0)) for job in succeeded
    )
    resolution_ratio = (resolved / total_refs) if total_refs else 0.0

    system_issues = sum(
        int((job.get("summary") or {}).get("system_issues", 0)) for job in succeeded
    )
    findings = sum(int((job.get("summary") or {}).get("findings", 0)) for job in succeeded)
    system_issue_ratio = (system_issues / findings) if findings else 0.0

    total_cost = round(sum(float(event.get("cost_usd") or 0.0) for event in telemetry), 6)
    cost_per_audit = round(total_cost / len(succeeded), 6) if succeeded else 0.0

    results = [
        _result(
            "audit_latency_p95",
            "Audit execution, 95th percentile",
            float(config.SLO_AUDIT_P95_SECONDS),
            percentile(run_times, 0.95),
            "seconds",
            "at_most",
            len(run_times),
            "Time from worker claim to a published evidence artifact.",
        ),
        _result(
            "queue_wait_p95",
            "Queue wait, 95th percentile",
            float(config.SLO_QUEUE_WAIT_P95_SECONDS),
            percentile(queue_waits, 0.95),
            "seconds",
            "at_most",
            len(queue_waits),
            "Time from submission to the first worker claim.",
        ),
        _result(
            "audit_success_ratio",
            "Audits completing without a runtime failure",
            float(config.SLO_AUDIT_SUCCESS_RATIO),
            round(success_ratio, 4),
            "ratio",
            "at_least",
            len(finished),
            "Cancelled audits count against this objective; provider outages do too.",
        ),
        _result(
            "reference_resolution_ratio",
            "References resolved to a source identity",
            float(config.SLO_RESOLUTION_RATIO),
            round(resolution_ratio, 4),
            "ratio",
            "at_least",
            total_refs,
            "Resolution is identity evidence, not a correctness judgement.",
        ),
        _result(
            "system_issue_ratio",
            "Findings caused by system issues rather than the manuscript",
            float(config.SLO_SYSTEM_ISSUE_RATIO),
            round(system_issue_ratio, 4),
            "ratio",
            "at_most",
            findings,
            "A high value means reviewers are triaging our failures, not their citations.",
        ),
        _result(
            "cost_per_audit",
            "Provider cost per completed audit",
            float(config.SLO_COST_PER_AUDIT_USD),
            cost_per_audit,
            "usd",
            "at_most",
            len(succeeded),
            "Estimated from recorded token counts and the configured price table.",
        ),
    ]

    stage_latency: dict[str, list[float]] = {}
    for event in telemetry:
        stage_latency.setdefault(str(event.get("stage") or "unknown"), []).append(
            float(event.get("duration_ms") or 0) / 1000.0
        )

    totals = {
        "completed_audits": len(finished),
        "succeeded_audits": len(succeeded),
        "failed_audits": len(finished) - len(succeeded),
        "total_cost_usd": total_cost,
        "cost_per_audit_usd": cost_per_audit,
        "total_input_tokens": sum(int(event.get("input_tokens") or 0) for event in telemetry),
        "total_output_tokens": sum(int(event.get("output_tokens") or 0) for event in telemetry),
        "provider_requests": sum(int(event.get("requests") or 0) for event in telemetry),
        "queue": queue,
        "stage_latency_p50_seconds": {
            stage: percentile(values, 0.50) for stage, values in sorted(stage_latency.items())
        },
        "stage_latency_p95_seconds": {
            stage: percentile(values, 0.95) for stage, values in sorted(stage_latency.items())
        },
    }
    return SloReport(window, utc_now(), results, totals)


__all__ = ["SloReport", "SloResult", "evaluate_slos", "percentile"]

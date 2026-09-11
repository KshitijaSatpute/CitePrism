"""Automatic repair for system issues.

`build_review` marks a finding `system_issue=True` when the cause is CitePrism's
own failure rather than anything about the manuscript. Phase 3 surfaced those
three issues to reviewers and asked them to rerun the whole audit by hand, which
is both slow and the wrong division of labour: a reviewer's attention should go
to citations, not to our provider outages.

The three system issues, and what actually fixes each:

======================================  ================================================
`bibliography_review` (linking failure)  Re-extract with the current reference matcher.
`identity_unresolved` (provider down)    Re-resolve identity; providers recover.
`assessment_error`                       Re-run assessment for the affected references.
======================================  ================================================

A repair is a *new audit* with targeted force flags. It never edits a stored
artifact: the original run stays readable, the repair produces a new one, and
the activity trail records both. That keeps the Phase 1 promise that evidence is
appended, never rewritten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

REPAIR_METHOD_VERSION = "repair-v1"

# Force flags consumed by PipelineOrchestrator.process_document.
STAGE_FLAGS = {
    "reextract_bibliography": {
        "force_parse": True,
        "force_enrich": True,
        "force_score": True,
    },
    "reresolve_identity": {
        "force_parse": False,
        "force_enrich": True,
        "force_score": True,
    },
    "rerun_assessment": {
        "force_parse": False,
        "force_enrich": False,
        "force_score": True,
    },
}


@dataclass(frozen=True)
class RepairPlan:
    """A proposed fix for one system issue."""

    issue_category: str
    action: str
    title: str
    explanation: str
    expected_outcome: str
    finding_ids: list[str] = field(default_factory=list)
    reference_ids: list[str] = field(default_factory=list)
    automatic: bool = True
    caveat: str | None = None

    @property
    def options(self) -> dict:
        return dict(STAGE_FLAGS.get(self.action, {}))

    def as_dict(self) -> dict:
        return {
            "issue_category": self.issue_category,
            "action": self.action,
            "title": self.title,
            "explanation": self.explanation,
            "expected_outcome": self.expected_outcome,
            "finding_ids": self.finding_ids,
            "reference_ids": self.reference_ids,
            "affected_findings": len(self.finding_ids),
            "automatic": self.automatic,
            "caveat": self.caveat,
            "options": self.options,
            "method_version": REPAIR_METHOD_VERSION,
        }


PLAN_TEMPLATES = {
    "bibliography_review": {
        "action": "reextract_bibliography",
        "title": "Re-extract the bibliography",
        "explanation": (
            "Parsed references could not be linked to their printed entries. This "
            "is an extraction failure on our side, not a defect in the manuscript."
        ),
        "expected_outcome": (
            "The updated matcher re-links entries. Printed numbering is preserved, "
            "and entries that stay ambiguous are still flagged for a person."
        ),
        "caveat": (
            "Re-extraction re-runs the language model parse, so this repair has a "
            "provider cost and may take several minutes."
        ),
    },
    "identity_unresolved": {
        "action": "reresolve_identity",
        "title": "Retry source identity resolution",
        "explanation": (
            "A scholarly index was unreachable when this audit ran, so identity "
            "could not be established. The reference itself may be perfectly fine."
        ),
        "expected_outcome": (
            "Identity resolution runs again across every enabled index. Only "
            "references that are still unresolved will remain in the queue."
        ),
        "caveat": None,
    },
    "assessment_error": {
        "action": "rerun_assessment",
        "title": "Run the citation assessment again",
        "explanation": (
            "The assessment provider returned a response that failed its claim or "
            "quotation checks. No support verdict was accepted from that attempt."
        ),
        "expected_outcome": (
            "Assessment runs again against the stored source snapshots. Cached "
            "parsing and enrichment are reused, so this is the cheapest repair."
        ),
        "caveat": (
            "If the same check fails again, the cause is unlikely to be transient "
            "and the reference needs human evidence review."
        ),
    },
}


def plan_repairs(review: dict) -> list[RepairPlan]:
    """Derive one repair plan per distinct system issue in a review projection."""
    grouped: dict[str, dict] = {}
    for finding in review.get("findings", []):
        if not finding.get("system_issue"):
            continue
        category = finding.get("category")
        if category not in PLAN_TEMPLATES:
            continue
        bucket = grouped.setdefault(category, {"findings": [], "references": []})
        bucket["findings"].append(finding["id"])
        if finding.get("reference_id"):
            bucket["references"].append(finding["reference_id"])

    plans = []
    for category, bucket in grouped.items():
        template = PLAN_TEMPLATES[category]
        plans.append(
            RepairPlan(
                issue_category=category,
                action=template["action"],
                title=template["title"],
                explanation=template["explanation"],
                expected_outcome=template["expected_outcome"],
                caveat=template["caveat"],
                finding_ids=sorted(set(bucket["findings"])),
                reference_ids=sorted(set(bucket["references"])),
            )
        )
    # Cheapest, most targeted repair first.
    order = {"assessment_error": 0, "identity_unresolved": 1, "bibliography_review": 2}
    return sorted(plans, key=lambda plan: order.get(plan.issue_category, 99))


def merge_options(plans: list[RepairPlan]) -> dict:
    """Combine several plans into one audit request.

    Running three separate audits to fix three issues would triple the cost and
    produce three artifacts a reviewer has to reconcile. Forcing the union of the
    required stages fixes all of them in one run.
    """
    options = {
        "force_parse": False,
        "force_enrich": False,
        "force_score": False,
        "run_missing_citations": False,
    }
    for plan in plans:
        for key, value in plan.options.items():
            options[key] = options[key] or bool(value)
    return options


class RepairService:
    """Plan and execute repairs against a completed audit."""

    def __init__(self, service, store, runner=None):
        self.service = service
        self.store = store
        self.runner = runner

    def plan(self, tenant_id: str, audit_id: str) -> dict:
        review = self.service.review(tenant_id, audit_id)
        plans = plan_repairs(review)
        history = self.store.list_repairs(tenant_id, audit_id)
        attempts: dict[str, int] = {}
        for record in history:
            key = str(record.get("issue_category"))
            attempts[key] = max(attempts.get(key, 0), int(record.get("attempt") or 1))
        payload = []
        for plan in plans:
            item = plan.as_dict()
            item["previous_attempts"] = attempts.get(plan.issue_category, 0)
            if item["previous_attempts"] >= 2:
                item["automatic"] = False
                item["caveat"] = (
                    "This repair has already been attempted twice without clearing "
                    "the issue. A person should inspect the evidence before another "
                    "automatic retry."
                )
            payload.append(item)
        return {
            "audit_id": audit_id,
            "system_issue_count": int(review["counts"].get("system_issues", 0)),
            "plans": payload,
            "history": history,
            "note": (
                "A repair starts a new audit and never modifies the stored artifact. "
                "The original run stays available for comparison."
            ),
        }

    def execute(
        self,
        tenant_id: str,
        audit_id: str,
        categories: list[str],
        *,
        actor_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        review = self.service.review(tenant_id, audit_id)
        available = {plan.issue_category: plan for plan in plan_repairs(review)}
        requested = [category for category in categories if category in available]
        skipped = sorted(set(categories) - set(requested))
        if not requested:
            raise ValueError(
                "none of the requested categories are open system issues on this audit"
            )

        audit = self.store.get_audit(tenant_id, audit_id)
        plans = [available[category] for category in requested]
        options = merge_options(plans)

        from src.workspace.models import AuditOptions

        history = self.store.list_repairs(tenant_id, audit_id)
        attempt = 1 + max(
            [
                int(record.get("attempt") or 1)
                for record in history
                if record.get("issue_category") in requested
            ]
            or [0]
        )

        new_audit, job = self.service.create_audit(
            tenant_id,
            audit["manuscript_id"],
            AuditOptions(**options),
            actor_id,
        )

        records = []
        for index, plan in enumerate(plans):
            key = f"{idempotency_key}:{plan.issue_category}" if idempotency_key else None
            records.append(
                self.store.record_repair(
                    tenant_id,
                    audit_id=audit_id,
                    finding_id=plan.finding_ids[0] if plan.finding_ids else None,
                    issue_category=plan.issue_category,
                    action=plan.action,
                    state="queued",
                    attempt=attempt,
                    follow_up_job_id=job.id,
                    follow_up_audit_id=new_audit.id,
                    detail={
                        "options": options,
                        "finding_ids": plan.finding_ids,
                        "reference_ids": plan.reference_ids,
                        "method_version": REPAIR_METHOD_VERSION,
                        "plan_index": index,
                    },
                    requested_by=actor_id,
                    idempotency_key=key,
                )
            )

        if self.runner is not None:
            try:
                self.runner.submit(tenant_id, job.id)
            except Exception:  # noqa: BLE001 - the durable queue picks it up anyway
                logger.exception("Direct repair submission failed; the worker will poll it.")

        return {
            "repairs": records,
            "audit": new_audit,
            "job": job,
            "repaired_categories": requested,
            "skipped_categories": skipped,
            "options": options,
            "note": (
                "A new audit is running. The original audit and its evidence "
                "artifact are unchanged and remain open for comparison."
            ),
        }

    def reconcile(self, tenant_id: str, follow_up_audit_id: str) -> int:
        """Close out repair records once the follow-up audit has finished."""
        try:
            follow_up = self.store.get_audit(tenant_id, follow_up_audit_id)
        except Exception:  # noqa: BLE001
            return 0
        if follow_up["state"] not in {"completed", "completed_with_issues", "failed"}:
            return 0

        remaining: dict[str, int] = {}
        if follow_up["state"] != "failed":
            try:
                review = self.service.review(tenant_id, follow_up_audit_id)
                for finding in review.get("findings", []):
                    if finding.get("system_issue"):
                        category = str(finding.get("category"))
                        remaining[category] = remaining.get(category, 0) + 1
            except Exception:  # noqa: BLE001
                logger.exception("Could not project the repaired audit for reconciliation.")
                return 0

        updated = 0
        for record in self.store.list_repairs(tenant_id):
            if record.get("follow_up_audit_id") != follow_up_audit_id:
                continue
            if record.get("state") not in {"queued", "running"}:
                continue
            category = str(record.get("issue_category"))
            if follow_up["state"] == "failed":
                state, note = "failed", "The repair audit did not complete."
            elif remaining.get(category):
                state = "failed"
                note = (
                    f"The repair ran, but {remaining[category]} finding(s) in this "
                    "category remain. Human review is needed."
                )
            else:
                state, note = "succeeded", "The system issue is no longer reported."
            detail = dict(record.get("detail") or {})
            detail["outcome"] = note
            detail["remaining_findings"] = remaining.get(category, 0)
            self.store.update_repair(tenant_id, record["id"], state=state, detail=detail)
            updated += 1
        return updated


__all__ = [
    "PLAN_TEMPLATES",
    "REPAIR_METHOD_VERSION",
    "RepairPlan",
    "RepairService",
    "STAGE_FLAGS",
    "merge_options",
    "plan_repairs",
]

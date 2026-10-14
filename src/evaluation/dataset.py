"""Labeled evaluation datasets stored as JSON Lines.

One line is one human judgement about one reference in one manuscript. The
format is deliberately small and explicit so a domain expert can produce it in a
spreadsheet, and so a disagreement between two annotators is recorded rather
than averaged away.

Ground truth is the annotator's, never the model's. Nothing in this module reads
a CitePrism output; the runner joins the two.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

DATASET_SCHEMA_VERSION = "citeprism-eval-v1"

# What a human can label about a reference.
IDENTITY_LABELS = ("resolved_correct", "resolved_incorrect", "unresolvable", "not_applicable")
SUPPORT_LABELS = ("supports", "contradicts", "context_only", "unrelated", "insufficient_evidence",
                  "unassessable")
EXTRACTION_LABELS = ("clean", "needs_review", "unlocatable")
# V2.0 objectives: relevance of the citation to the claims it supports,
# self-citation, and bibliographic metadata consistency.
RELEVANCE_LABELS = ("relevant", "borderline", "irrelevant")
METADATA_LABELS = ("consistent", "inconsistent")


@dataclass
class LabeledItem:
    """One annotated reference."""

    item_id: str
    manuscript_sha256: str
    reference_marker: str
    # Optional free text so an annotator can find the entry again by eye.
    reference_title: str | None = None
    identity_label: str | None = None
    support_label: str | None = None
    extraction_label: str | None = None
    relevance_label: str | None = None
    metadata_label: str | None = None
    # True when the reference shares an author with the manuscript.
    is_self_citation: bool | None = None
    # True when a reviewer would want this reference surfaced at all.
    should_be_reviewed: bool | None = None
    # Research field of the manuscript, for multi-domain breakdowns.
    domain: str | None = None
    annotator: str = "unknown"
    annotated_at: str | None = None
    notes: str | None = None
    metadata: dict = field(default_factory=dict)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.item_id:
            problems.append("item_id is required")
        if not self.manuscript_sha256 or len(self.manuscript_sha256) != 64:
            problems.append(f"{self.item_id}: manuscript_sha256 must be a SHA-256 digest")
        if not self.reference_marker:
            problems.append(f"{self.item_id}: reference_marker is required")
        for value, allowed, name in (
            (self.identity_label, IDENTITY_LABELS, "identity_label"),
            (self.support_label, SUPPORT_LABELS, "support_label"),
            (self.extraction_label, EXTRACTION_LABELS, "extraction_label"),
            (self.relevance_label, RELEVANCE_LABELS, "relevance_label"),
            (self.metadata_label, METADATA_LABELS, "metadata_label"),
        ):
            if value is not None and value not in allowed:
                problems.append(f"{self.item_id}: {name} '{value}' is not a permitted label")
        if all(value is None for value in (
            self.identity_label, self.support_label, self.extraction_label,
            self.relevance_label, self.metadata_label, self.is_self_citation,
            self.should_be_reviewed,
        )):
            problems.append(f"{self.item_id}: at least one label is required")
        return problems

    def as_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class EvaluationDataset:
    name: str
    items: list[LabeledItem]
    schema_version: str = DATASET_SCHEMA_VERSION
    description: str = ""
    # Two annotators labeling the same item_id lets the runner report agreement.
    allow_duplicate_items: bool = True

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.schema_version != DATASET_SCHEMA_VERSION:
            problems.append(
                f"dataset schema {self.schema_version} is not {DATASET_SCHEMA_VERSION}"
            )
        if not self.items:
            problems.append("the dataset contains no labeled items")
        for item in self.items:
            problems.extend(item.validate())
        if not self.allow_duplicate_items:
            seen: set[str] = set()
            for item in self.items:
                if item.item_id in seen:
                    problems.append(f"duplicate item_id: {item.item_id}")
                seen.add(item.item_id)
        return problems

    def by_manuscript(self, digest: str) -> list[LabeledItem]:
        return [item for item in self.items if item.manuscript_sha256 == digest]

    def annotators(self) -> list[str]:
        return sorted({item.annotator for item in self.items})

    def summary(self) -> dict:
        return {
            "name": self.name,
            "schema_version": self.schema_version,
            "items": len(self.items),
            "manuscripts": len({item.manuscript_sha256 for item in self.items}),
            "annotators": self.annotators(),
            "labeled_identity": sum(item.identity_label is not None for item in self.items),
            "labeled_support": sum(item.support_label is not None for item in self.items),
            "labeled_extraction": sum(item.extraction_label is not None for item in self.items),
            "labeled_relevance": sum(item.relevance_label is not None for item in self.items),
            "labeled_self_citation": sum(item.is_self_citation is not None for item in self.items),
            "labeled_metadata": sum(item.metadata_label is not None for item in self.items),
            "domains": sorted({item.domain for item in self.items if item.domain}),
        }


def load_dataset(path: str | Path, *, name: str | None = None) -> EvaluationDataset:
    """Read a JSONL dataset. A leading object with `schema_version` is a header."""
    path = Path(path)
    items: list[LabeledItem] = []
    schema_version = DATASET_SCHEMA_VERSION
    description = ""
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path.name} line {number} is not valid JSON") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path.name} line {number} is not a JSON object")
            if "schema_version" in record and "item_id" not in record:
                schema_version = str(record["schema_version"])
                description = str(record.get("description", ""))
                continue
            known = set(LabeledItem.__dataclass_fields__)
            extra = {key: value for key, value in record.items() if key not in known}
            fields = {key: value for key, value in record.items() if key in known}
            if extra:
                fields.setdefault("metadata", {}).update(extra)
            items.append(LabeledItem(**fields))
    return EvaluationDataset(
        name=name or path.stem,
        items=items,
        schema_version=schema_version,
        description=description,
    )


def write_dataset(dataset: EvaluationDataset, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "schema_version": dataset.schema_version,
                "name": dataset.name,
                "description": dataset.description,
            },
            ensure_ascii=False,
        )
    ]
    lines.extend(json.dumps(item.as_dict(), ensure_ascii=False) for item in dataset.items)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


__all__ = [
    "METADATA_LABELS",
    "RELEVANCE_LABELS",
    "DATASET_SCHEMA_VERSION",
    "EXTRACTION_LABELS",
    "EvaluationDataset",
    "IDENTITY_LABELS",
    "LabeledItem",
    "SUPPORT_LABELS",
    "load_dataset",
    "write_dataset",
]

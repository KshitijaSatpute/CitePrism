"""Small offline inputs; PDF bytes test registration, not PDF parsing accuracy."""
import json
from pathlib import Path
from uuid import uuid4

from src.trust.models import Actor
from src.trust.object_store import LocalObjectStore
from src.trust.service import TrustKernel


def make_kernel(root, repository, tenant=None):
    tenant = tenant or uuid4()
    return TrustKernel(repository, LocalObjectStore(Path(root) / "objects", namespace=str(tenant)),
                       tenant_id=tenant, actor=Actor(actor_type="service", actor_id="test-runner"))


def make_pdf(root, name="paper.pdf", body=b"first"):
    path = Path(root) / name
    path.write_bytes(b"%PDF-1.4\n" + body + b"\n%%EOF\n")
    return path


def make_scored(root, *, references=None):
    value = {"scoring_metadata": {"generated_at": "2026-09-08T00:00:00Z"},
             "scored_references": references if references is not None else [
                 {"ref_id": "[1]", "original_data": {"parsed": {"title": "Example"}},
                  "external_metadata": {"id": "https://openalex.org/W1"},
                  "RS_final": None, "quality_flags": ["Missing DOI", "missing-doi"]}
             ]}
    path = Path(root) / "scored.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path

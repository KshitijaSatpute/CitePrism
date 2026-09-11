"""Signed review packages in PDF, HTML and JSON.

An export is the artifact that leaves the system, so it carries everything a
recipient needs to assess it without access to CitePrism: the review projection,
the human decisions, the correction proposals, the activity hash-chain head, the
scope limitations, and a manifest signed over all of it.

The PDF is generated with a small, dependency-light writer rather than a heavy
reporting stack, because a review package must still build on a locked-down
runner where installing a rendering toolchain is not an option.
"""

from __future__ import annotations

import html
import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from src.evidence.review import assessment_diagnostics, build_review, report_html, status_label
from src.workspace.common import utc_now
from src.workspace.objects import content_key, digest_bytes
from src.workspace.signing import Signer

logger = logging.getLogger(__name__)

EXPORT_VERSION = "review-package-v1"
FORMATS = ("json", "html", "pdf", "bundle")

LIMITATIONS = (
    "Automated proposals in this package are unreviewed model output. They are "
    "not findings of error, bias or research misconduct.",
    "A located quotation proves the text appears in the stored source snapshot. "
    "It does not establish that the citing interpretation is correct.",
    "Reference resolution is rule-based identity evidence. Agreement between "
    "scholarly indexes is recorded as evidence, not as a calibrated probability.",
    "This package reflects the audit at the moment of export. Later decisions, "
    "corrections or repairs are not included.",
)


# ---------------------------------------------------------------------
# Minimal PDF writer
# ---------------------------------------------------------------------
class SimplePdf:
    """A deliberately small PDF/1.4 writer for text reports.

    Uses only the base-14 Helvetica fonts, so there is no font embedding, no
    external binary and no network access. It handles pagination, headings,
    body text and simple key/value rows — which is the whole shape of a review
    package.
    """

    PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89  # A4 points
    MARGIN = 56.0

    def __init__(self, title: str):
        self.title = title
        self.pages: list[list[str]] = []
        self._current: list[str] = []
        self._y = self.PAGE_HEIGHT - self.MARGIN
        self._new_page()

    def _new_page(self) -> None:
        if self._current:
            self.pages.append(self._current)
        self._current = []
        self._y = self.PAGE_HEIGHT - self.MARGIN

    @staticmethod
    def _escape(text: str) -> str:
        return (
            str(text)
            .replace("\\", r"\\")
            .replace("(", r"\(")
            .replace(")", r"\)")
        )

    @staticmethod
    def _latin1(text: str) -> str:
        # Base-14 fonts are single byte; keep the document readable rather than
        # failing on a non-Latin character.
        return str(text).encode("latin-1", "replace").decode("latin-1")

    def _wrap(self, text: str, size: float, width: float) -> list[str]:
        # Helvetica averages about 0.5 em per character; 0.52 is a safe estimate.
        per_char = size * 0.52
        limit = max(10, int(width / per_char))
        words, lines, line = str(text).split(), [], ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) <= limit:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = word if len(word) <= limit else word[: limit - 1] + "-"
        if line:
            lines.append(line)
        return lines or [""]

    def text(self, content: str, *, size: float = 10.5, bold: bool = False,
             leading: float | None = None, gap: float = 0.0) -> None:
        leading = leading or size * 1.35
        font = "F2" if bold else "F1"
        width = self.PAGE_WIDTH - 2 * self.MARGIN
        for line in self._wrap(self._latin1(content), size, width):
            if self._y - leading < self.MARGIN:
                self._new_page()
            self._current.append(
                f"BT /{font} {size} Tf {self.MARGIN} {self._y:.2f} Td "
                f"({self._escape(line)}) Tj ET"
            )
            self._y -= leading
        self._y -= gap

    def heading(self, content: str, *, level: int = 1) -> None:
        size = {1: 18.0, 2: 13.5, 3: 11.5}.get(level, 11.0)
        if self._y < self.MARGIN + 80:
            self._new_page()
        else:
            self._y -= 6
        self.text(content, size=size, bold=True, gap=4)

    def rule(self) -> None:
        if self._y - 12 < self.MARGIN:
            self._new_page()
        self._y -= 6
        self._current.append(
            f"0.75 w 0.55 0.6 0.65 RG {self.MARGIN} {self._y:.2f} m "
            f"{self.PAGE_WIDTH - self.MARGIN} {self._y:.2f} l S"
        )
        self._y -= 10

    def field(self, label: str, value: str) -> None:
        self.text(f"{label}: {value}", size=10.0, leading=13.5)

    def bullet(self, content: str) -> None:
        self.text(f"\u2022  {content}", size=10.0, leading=13.5)

    def spacer(self, amount: float = 8.0) -> None:
        self._y -= amount

    def render(self) -> bytes:
        self.pages.append(self._current)
        pages = [page for page in self.pages if page] or [[]]
        objects: list[bytes] = []

        def add(payload: bytes) -> int:
            objects.append(payload)
            return len(objects)

        font_regular = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        )
        font_bold = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>"
        )
        pages_id = len(objects) + 1 + 2 * len(pages) + 1
        page_ids, content_ids = [], []
        for page in pages:
            stream = "\n".join(page).encode("latin-1", "replace")
            content_ids.append(
                add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                    + stream + b"\nendstream")
            )
        for index, content_id in enumerate(content_ids):
            page_ids.append(
                add(
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox "
                    f"[0 0 {self.PAGE_WIDTH:.2f} {self.PAGE_HEIGHT:.2f}] "
                    f"/Resources << /Font << /F1 {font_regular} 0 R "
                    f"/F2 {font_bold} 0 R >> >> /Contents {content_id} 0 R >>".encode()
                )
            )
            del index
        kids = " ".join(f"{identifier} 0 R" for identifier in page_ids)
        actual_pages_id = add(
            f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
        )
        info_id = add(
            b"<< /Title (" + self._escape(self._latin1(self.title)).encode("latin-1", "replace")
            + b") /Producer (CitePrism review package) >>"
        )
        catalog_id = add(f"<< /Type /Catalog /Pages {actual_pages_id} 0 R >>".encode())

        buffer = io.BytesIO()
        buffer.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, payload in enumerate(objects, 1):
            offsets.append(buffer.tell())
            buffer.write(f"{number} 0 obj\n".encode() + payload + b"\nendobj\n")
        start = buffer.tell()
        buffer.write(f"xref\n0 {len(objects) + 1}\n".encode())
        buffer.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            buffer.write(f"{offset:010d} 00000 n \n".encode())
        buffer.write(
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R "
            f"/Info {info_id} 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
        )
        return buffer.getvalue()


# ---------------------------------------------------------------------
# Package content
# ---------------------------------------------------------------------
def review_package(review: dict, *, audit: dict, manuscript: dict, activity: dict,
                   exported_by: str) -> dict:
    """The JSON review package: everything a recipient needs, nothing more."""
    return {
        "package_version": EXPORT_VERSION,
        "generated_at": utc_now(),
        "exported_by": exported_by,
        "manuscript": {
            "id": manuscript.get("id"),
            "title": manuscript.get("title"),
            "original_filename": manuscript.get("original_filename"),
            "content_sha256": manuscript.get("content_sha256"),
            "text_layer_status": manuscript.get("text_layer_status", "unknown"),
        },
        "audit": {
            "id": audit.get("id"),
            "state": audit.get("state"),
            "created_at": audit.get("created_at"),
            "completed_at": audit.get("completed_at"),
            "summary": audit.get("summary", {}),
        },
        "review": review,
        "activity_verification": activity,
        "limitations": list(LIMITATIONS) + list(review.get("limitations", [])),
    }


def package_pdf(package: dict, artifact: dict) -> bytes:
    """Render the review package as a paginated PDF."""
    review = package["review"]
    counts = review.get("counts", {})
    pdf = SimplePdf(f"CitePrism review — {review.get('title', 'Untitled')}")

    pdf.heading("CitePrism evidence review", level=1)
    pdf.text(review.get("title", "Untitled manuscript"), size=12.5, bold=True, gap=2)
    pdf.rule()
    pdf.field("Audit", str(package["audit"]["id"]))
    pdf.field("Processing status", status_label(review.get("processing_status", "unknown")))
    pdf.field("Human review status", status_label(review.get("review_status", "not_reviewed")))
    pdf.field("Completed", str(package["audit"].get("completed_at") or "Not recorded"))
    pdf.field("Manuscript SHA-256", str(package["manuscript"].get("content_sha256") or "unknown"))
    pdf.field("Exported", f"{package['generated_at']} by {package['exported_by']}")
    verification = package.get("activity_verification") or {}
    pdf.field(
        "Activity chain",
        f"{'intact' if verification.get('valid') else 'FAILED VERIFICATION'} "
        f"({verification.get('event_count', 0)} events)",
    )
    pdf.spacer()

    pdf.heading("Summary", level=2)
    for label, key in (
        ("References", "total_references"),
        ("Citation occurrences located", "citation_occurrences"),
        ("Resolved to a source identity", "resolved_references"),
        ("Unresolved", "unresolved_references"),
        ("Grounded model proposals", "grounded_proposals"),
        ("References with review items", "references_to_review"),
        ("Findings", "findings"),
        ("System issues", "system_issues"),
        ("Recorded reviewer decisions", "reviewer_decisions"),
    ):
        pdf.field(label, str(counts.get(key, 0)))
    pdf.spacer()

    pdf.heading("Review items", level=2)
    titles = {
        item["id"]: f"{item['marker']} {item['title']}" for item in review.get("references", [])
    }
    findings = review.get("findings", [])
    if not findings:
        pdf.text("No review items were raised for this manuscript.")
    for finding in findings:
        subject = titles.get(finding.get("reference_id"), "Manuscript-level")
        pdf.text(
            f"[{finding['priority'].upper()}] {subject}", size=10.5, bold=True, leading=13.5
        )
        pdf.text(finding["summary"], size=10.0, leading=13.0)
        pdf.text(finding["explanation"], size=9.5, leading=12.5)
        pdf.text(f"Next step: {finding['action']}", size=9.5, leading=12.5)
        decision = finding.get("decision")
        if decision:
            pdf.text(
                f"Decision: {status_label(decision['disposition'])} by "
                f"{decision['reviewer_id']} — {decision['rationale']}",
                size=9.5,
                leading=12.5,
            )
        else:
            pdf.text("Decision: not yet recorded", size=9.5, leading=12.5)
        if finding.get("system_issue"):
            pdf.text(
                "This item reports a CitePrism processing failure, not a defect "
                "in the manuscript.",
                size=9.0,
                leading=12.0,
            )
        pdf.spacer(5)

    corrections = review.get("corrections") or []
    if corrections:
        pdf.heading("Correction proposals", level=2)
        for correction in corrections:
            pdf.text(
                f"{correction['kind']} — {status_label(correction['state'])}",
                size=10.0,
                bold=True,
                leading=13.0,
            )
            pdf.text(f"Rationale: {correction['rationale']}", size=9.5, leading=12.5)
            pdf.text(
                f"Proposed: {json.dumps(correction.get('proposed_value', {}), ensure_ascii=False)}",
                size=9.5,
                leading=12.5,
            )
            pdf.spacer(4)

    diagnostics = assessment_diagnostics(artifact)
    if diagnostics:
        pdf.heading("Assessment checks", level=2)
        pdf.text(
            "A successful provider call is not an accepted assessment. Each "
            "response must also pass its claim and quotation checks.",
            size=9.5,
        )
        for item in diagnostics[:120]:
            pdf.bullet(
                f"{item['reference']}: {status_label(item['status'])} — {item['detail']}"
            )
        pdf.spacer()

    pdf.heading("Scope and limitations", level=2)
    for limitation in package["limitations"]:
        pdf.bullet(limitation)

    return pdf.render()


def package_html(package: dict, artifact: dict) -> bytes:
    """HTML export, reusing the Phase 2 report and adding the human layer."""
    base = report_html(artifact)
    review = package["review"]
    escape = lambda value: html.escape(str(value), quote=True)  # noqa: E731

    decisions = "".join(
        f"<tr><td>{escape(finding['summary'])}</td>"
        f"<td>{escape(status_label(finding['decision']['disposition']))}</td>"
        f"<td>{escape(finding['decision']['reviewer_id'])}</td>"
        f"<td>{escape(finding['decision']['rationale'])}</td></tr>"
        for finding in review.get("findings", [])
        if finding.get("decision")
    )
    verification = package.get("activity_verification") or {}
    header = (
        "<section><h2>Review package</h2>"
        f"<p>Audit {escape(package['audit']['id'])} · exported "
        f"{escape(package['generated_at'])} by {escape(package['exported_by'])}</p>"
        f"<p>Manuscript SHA-256: <code>{escape(package['manuscript']['content_sha256'])}</code></p>"
        f"<p>Activity chain: {'intact' if verification.get('valid') else 'FAILED VERIFICATION'} "
        f"({escape(verification.get('event_count', 0))} events)</p>"
        + (
            "<h3>Recorded reviewer decisions</h3><table><thead><tr><th>Item</th>"
            "<th>Disposition</th><th>Reviewer</th><th>Rationale</th></tr></thead>"
            f"<tbody>{decisions}</tbody></table>"
            if decisions
            else "<p>No reviewer decisions were recorded before this export.</p>"
        )
        + "</section>"
    )
    marker = "<h2>Scope and limitations</h2>"
    body = base.replace(marker, header + marker) if marker in base else base + header
    return body.encode("utf-8")


def package_json(package: dict) -> bytes:
    return json.dumps(package, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def build_manifest(package: dict, files: list[dict], *, tenant_id: str) -> dict:
    return {
        "manifest_version": EXPORT_VERSION,
        "tenant_id": tenant_id,
        "audit_id": package["audit"]["id"],
        "manuscript_sha256": package["manuscript"]["content_sha256"],
        "generated_at": package["generated_at"],
        "exported_by": package["exported_by"],
        "review_status": package["review"].get("review_status"),
        "processing_status": package["review"].get("processing_status"),
        "counts": package["review"].get("counts", {}),
        "activity_head_hash": (package.get("activity_verification") or {}).get("head_hash"),
        "activity_chain_valid": (package.get("activity_verification") or {}).get("valid"),
        "files": files,
        "limitations": package["limitations"],
    }


class ExportService:
    """Build, sign and store review packages."""

    def __init__(self, service, store, object_store, *, signer: Signer | None = None,
                 retention_days: int = 365, tenant_namespace: str | None = None):
        self.service = service
        self.store = store
        self.object_store = object_store
        self.signer = signer or Signer("ed25519")
        self.retention_days = retention_days
        self.tenant_namespace = tenant_namespace

    def create(self, tenant_id: str, audit_id: str, export_format: str, *, actor_id: str) -> dict:
        if export_format not in FORMATS:
            raise ValueError(f"unsupported export format: {export_format}")

        artifact = self.service.load_artifact(tenant_id, audit_id)
        review = self.service.review(tenant_id, audit_id)
        audit = self.service.get_audit(tenant_id, audit_id)
        audit_row = audit.model_dump(mode="json") if hasattr(audit, "model_dump") else dict(audit)
        manuscript = self.service.get_manuscript(tenant_id, audit_row["manuscript_id"])
        manuscript_row = (
            manuscript.model_dump(mode="json")
            if hasattr(manuscript, "model_dump")
            else dict(manuscript)
        )
        activity = self.store.verify_activity_chain(tenant_id)

        package = review_package(
            review,
            audit=audit_row,
            manuscript=manuscript_row,
            activity=activity,
            exported_by=actor_id,
        )

        renditions: dict[str, tuple[bytes, str]] = {
            "json": (package_json(package), "application/json"),
        }
        if export_format in {"html", "pdf", "bundle"}:
            renditions["html"] = (package_html(package, artifact), "text/html; charset=utf-8")
        if export_format in {"pdf", "bundle"}:
            renditions["pdf"] = (package_pdf(package, artifact), "application/pdf")

        files = [
            {
                "name": f"review.{name}",
                "media_type": media_type,
                "size_bytes": len(payload),
                "sha256": digest_bytes(payload),
            }
            for name, (payload, media_type) in sorted(renditions.items())
        ]
        manifest = build_manifest(package, files, tenant_id=tenant_id)
        signature = self.signer.sign_manifest(manifest)
        manifest_with_signature = {**manifest, "signature": signature.as_dict()}

        if export_format == "bundle":
            payload, media_type = self._zip(renditions, manifest_with_signature), "application/zip"
        else:
            payload, media_type = renditions[export_format]

        digest = digest_bytes(payload)
        namespace = self.tenant_namespace or tenant_id
        suffix = {
            "json": ".json",
            "html": ".html",
            "pdf": ".pdf",
            "bundle": ".zip",
        }[export_format]
        key = content_key(namespace, "exports", digest, suffix=suffix)
        reference = self.object_store.put_bytes(payload, key=key, media_type=media_type)

        record = self.store.record_export(
            tenant_id,
            audit_id=audit_id,
            format=export_format,
            object_backend=reference.backend,
            object_key=reference.key,
            object_version_id=reference.version_id,
            content_sha256=reference.content_sha256,
            size_bytes=reference.size_bytes,
            signature_algorithm=signature.algorithm,
            signature_key_id=signature.key_id,
            signature=signature.value,
            manifest=manifest_with_signature,
            created_by=actor_id,
            retention_days=self.retention_days,
        )
        record["media_type"] = media_type
        record["download_filename"] = f"citeprism-review-{str(audit_id)[:8]}{suffix}"
        return record

    @staticmethod
    def _zip(renditions: dict[str, tuple[bytes, str]], manifest: dict) -> bytes:
        buffer = io.BytesIO()
        # A fixed timestamp keeps the bundle reproducible: the same review always
        # produces the same bytes, so a recipient can compare two exports.
        fixed = (2026, 1, 1, 0, 0, 0)
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, (payload, _media_type) in sorted(renditions.items()):
                info = zipfile.ZipInfo(f"review.{name}", date_time=fixed)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, payload)
            info = zipfile.ZipInfo("manifest.json", date_time=fixed)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(
                info,
                json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False),
            )
            readme = zipfile.ZipInfo("README.txt", date_time=fixed)
            readme.external_attr = 0o644 << 16
            archive.writestr(readme, BUNDLE_README)
        return buffer.getvalue()

    def download(self, tenant_id: str, export_id: str) -> tuple[dict, bytes]:
        record = self.store.get_export(tenant_id, export_id)
        payload = self.object_store.get_bytes(
            record["object_key"], expected_sha256=record["content_sha256"]
        )
        return record, payload

    def signed_url(self, tenant_id: str, export_id: str) -> str | None:
        record = self.store.get_export(tenant_id, export_id)
        return self.object_store.presigned_url(record["object_key"])


BUNDLE_README = """CitePrism review package
========================

Contents
  review.json    The complete review projection, decisions and corrections.
  review.html    The same content as a portable, self-contained web page.
  review.pdf     A paginated copy for printing or filing.
  manifest.json  File digests plus a detached signature over this manifest.

How to verify this package
  1. Recompute the SHA-256 of each file and compare it with manifest.json.
  2. Verify manifest.signature over the manifest with every key EXCEPT the
     "signature" field, serialized as compact JSON with sorted keys.
  3. For an ed25519 signature, use the public key recorded in the manifest or
     the published deployment key. A key id ending in "-ephemeral" was generated
     at export time and cannot attribute this package to a deployment.

What this package does and does not claim
  Automated proposals are unreviewed model output, not findings of error, bias
  or research misconduct. A located quotation proves the text appears in the
  stored source snapshot; it does not establish that the citing interpretation
  is correct. Every item requires human judgement.
"""


__all__ = [
    "EXPORT_VERSION",
    "ExportService",
    "FORMATS",
    "SimplePdf",
    "build_manifest",
    "package_html",
    "package_json",
    "package_pdf",
    "review_package",
]

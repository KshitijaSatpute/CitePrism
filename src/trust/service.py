"""Trust-bearing ingestion, atomic legacy capture, and append-only review."""
import hashlib
import json
import math
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.trust.audit import verify_audit_chain
from src.trust.canonical import canonical_object, sha256_json, sha256_text
from src.trust.ids import derived_uuid, new_uuid7, utc_now
from src.trust.models import (
    Actor, CandidateMatch, DocumentRecord, DocumentVersionRecord, Evidence,
    Finding, IngestReceipt, LegacyCaptureReceipt, ModelRun, ReferenceRecord,
    ReviewerDecision, SourceSpan,
)
from src.trust.ports import TrustConflictError
from src.utils.safe_io import sha256_file, validate_pdf


def safe_filename(value):
    name = value.replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "_", name)
    return (name[:500] if name and name not in {".", ".."} else "manuscript.pdf")


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def category(flag):
    return re.sub(r"[^a-z0-9]+", "_", flag.lower()).strip("_")[:200] or "review_required"


class TrustKernel:
    def __init__(self, repository, object_store, *, tenant_id, actor, max_pdf_bytes=50 * 1024**2):
        self.repository, self.object_store = repository, object_store
        self.tenant_id, self.actor = tenant_id, actor
        self.max_pdf_bytes = max_pdf_bytes

    def _receipt(self, document, version, reused):
        return IngestReceipt(tenant_id=self.tenant_id, document_id=document.id,
                             document_version_id=version.id, content_sha256=version.content_sha256,
                             object_backend=version.object_backend, object_key=version.object_key,
                             object_version_id=version.object_version_id, reused=reused)

    def ingest_pdf(self, path, *, original_filename=None, title=None, external_key=None):
        path = Path(path).resolve()
        validate_pdf(path, max_bytes=self.max_pdf_bytes)
        digest = sha256_file(path)
        title = title.strip() if title and title.strip() else None
        external_key = external_key.strip() if external_key and external_key.strip() else None
        stored = self.object_store.put_file(path, content_sha256=digest, media_type="application/pdf")
        existing = self.repository.get_document_version_by_hash(self.tenant_id, digest)
        if existing:
            if external_key and existing[0].external_key != external_key:
                raise TrustConflictError("bytes already belong to another external document key")
            return self._receipt(*existing, reused=True)
        now = utc_now()
        previous = self.repository.get_latest_document_version_by_external_key(self.tenant_id, external_key) if external_key else None
        if previous:
            document = previous[0]
            if title is not None and title != document.title:
                raise TrustConflictError("document title is immutable")
        else:
            document = DocumentRecord(id=new_uuid7(now), tenant_id=self.tenant_id,
                                      title=title, external_key=external_key, created_at=now,
                                      created_by=self.actor.actor_id)
        version = DocumentVersionRecord(
            id=new_uuid7(now), tenant_id=self.tenant_id, document_id=document.id,
            version_number=previous[1].version_number + 1 if previous else 1,
            parent_version_id=previous[1].id if previous else None,
            content_sha256=digest, object_backend=stored.backend, object_key=stored.key,
            object_version_id=stored.version_id,
            original_filename=safe_filename(original_filename or path.name),
            size_bytes=stored.size_bytes, created_at=now, created_by=self.actor.actor_id,
        )
        try:
            self.repository.append_batch([document, version], self.actor)
        except TrustConflictError:
            # A concurrent identical upload is a retry; a different revision remains a conflict.
            existing = self.repository.get_document_version_by_hash(self.tenant_id, digest)
            if existing and existing[0].external_key == document.external_key:
                return self._receipt(*existing, reused=True)
            raise
        return self._receipt(document, version, reused=False)

    def _require_version(self, version_id):
        version = self.repository.get_record("document_version", self.tenant_id, version_id)
        if version is None:
            raise TrustConflictError("document version is absent in this tenant")
        return version

    def record_source_span(self, *, document_version_id, text, page_start=None,
                           page_end=None, char_start=None, char_end=None, locator=None):
        self._require_version(document_version_id)
        span = SourceSpan(id=new_uuid7(), tenant_id=self.tenant_id,
                          document_version_id=document_version_id, page_start=page_start,
                          page_end=page_end, char_start=char_start, char_end=char_end,
                          text_sha256=sha256_text(text), locator=canonical_object(locator or {}),
                          created_at=utc_now())
        self.repository.append_batch([span], self.actor)
        return span

    def record_reviewer_decision(self, *, finding_id, disposition, rationale,
                                 reviewer_id, supersedes_decision_id=None):
        # Caller must supply authenticated identity in a later API; not supplied by V2 UI.
        decision = ReviewerDecision(id=new_uuid7(), tenant_id=self.tenant_id,
                                    finding_id=finding_id, disposition=disposition,
                                    rationale=rationale, reviewer_id=reviewer_id,
                                    supersedes_decision_id=supersedes_decision_id, created_at=utc_now())
        self.repository.append_batch([decision], Actor(actor_type="human", actor_id=reviewer_id))
        return decision

    def capture_legacy_scored_artifact(self, document_version_id, scored_json_path):
        version = self._require_version(document_version_id)
        path = Path(scored_json_path)
        with path.open("rb") as handle:
            raw = handle.read(50 * 1024**2 + 1)
        if len(raw) > 50 * 1024**2:
            raise ValueError("legacy scored JSON exceeds 50 MiB")
        artifact = json.loads(raw)
        if not isinstance(artifact, dict) or not isinstance(artifact.get("scored_references"), list):
            raise ValueError("scored JSON requires a scored_references array")
        from src.evidence import SCHEMA_VERSION
        if artifact.get("schema_version") == SCHEMA_VERSION:
            from src.evidence.trust_capture import capture
            return capture(self, version, raw, artifact)
        scoring = artifact.get("scoring_metadata") or {}
        if not isinstance(scoring, dict):
            raise ValueError("scoring_metadata must be an object")
        artifact_hash = hashlib.sha256(raw).hexdigest()
        timestamp = version.created_at
        if scoring.get("generated_at"):
            try:
                parsed = datetime.fromisoformat(str(scoring["generated_at"]).replace("Z", "+00:00"))
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    timestamp = parsed.astimezone(timezone.utc)
            except ValueError:
                pass
        run_id = derived_uuid(document_version_id, "legacy-import", artifact_hash)
        # Preserve exact imported output bytes, not only a digest pointing at a mutable V2 file.
        with tempfile.TemporaryDirectory(prefix="citeprism-capture-") as directory:
            snapshot = Path(directory) / "scored.json"
            snapshot.write_bytes(raw)
            stored = self.object_store.put_file(snapshot, content_sha256=artifact_hash, media_type="application/json")
        run = ModelRun(
            id=run_id, tenant_id=self.tenant_id, document_version_id=document_version_id,
            purpose="legacy_score_import", provider=str(scoring.get("llm_provider") or "unrecorded"),
            model=str(scoring.get("llm_model") or "unrecorded"),
            prompt_version=str(scoring.get("prompt_version") or "legacy-unrecorded"),
            method_version=str(scoring.get("method_version") or "legacy-v2"),
            input_sha256=version.content_sha256, output_sha256=artifact_hash,
            configuration={"legacy_metadata": canonical_object(scoring),
                           "artifact_object": stored.model_dump(mode="json"),
                           "provider_execution_observed": False,
                           "timestamp_source": "legacy_or_document_fallback"},
            status="succeeded", started_at=timestamp, completed_at=timestamp,
        )
        records = [run]
        record_categories = ["runs"]
        seen_keys = set()
        threshold = number(scoring.get("borderline_threshold"))
        threshold = threshold if threshold is not None and 0 <= threshold <= 100 else 40
        for ordinal, item in enumerate(artifact["scored_references"], 1):
            if not isinstance(item, dict):
                raise ValueError("each scored reference must be an object")
            source_key = str(item.get("ref_id") or ordinal)
            key = re.sub(r"[\[\]\s]", "", source_key).lower()[:250] or str(ordinal)
            if key in seen_keys:
                raise ValueError(f"duplicate normalized reference key: {key}")
            seen_keys.add(key)
            original = item.get("original_data") or {}
            if not isinstance(original, dict) or not isinstance(original.get("parsed", {}), dict):
                raise ValueError("original_data and parsed metadata must be objects")
            reference_id = derived_uuid(document_version_id, "reference", key)
            reference = ReferenceRecord(
                id=reference_id, tenant_id=self.tenant_id, document_version_id=document_version_id,
                reference_key=key, ordinal=ordinal, raw_text_sha256=sha256_json(original),
                bibliographic_metadata={**canonical_object(original.get("parsed", {})), "source_reference_key": source_key},
                created_at=version.created_at,
            )
            records.append(reference)
            record_categories.append("references")
            external = item.get("external_metadata") or {}
            if not isinstance(external, dict):
                raise ValueError("external_metadata must be an object")
            external_id = external.get("id") or external.get("doi")
            candidate_id = None
            if external_id:
                confidence = number(external.get("match_confidence"))
                if confidence is not None and not 0 <= confidence <= 1:
                    confidence = None
                candidate_id = derived_uuid(run_id, "candidate", reference_id, str(external_id))
                records.append(CandidateMatch(
                    id=candidate_id, tenant_id=self.tenant_id, document_version_id=document_version_id,
                    reference_id=reference_id, model_run_id=run_id,
                    provider=str(external.get("provider") or "legacy-unrecorded"),
                    external_id=str(external_id), match_method=str(external.get("match_method") or "legacy-selected"),
                    confidence=confidence, selected=True, metadata=canonical_object(external), created_at=timestamp,
                ))
                record_categories.append("candidates")
            records.append(Evidence(
                id=derived_uuid(run_id, "score-evidence", key), tenant_id=self.tenant_id,
                document_version_id=document_version_id, subject_type="reference", subject_id=reference_id,
                kind="score_component", candidate_match_id=candidate_id, model_run_id=run_id,
                statement="Imported automated score components; source support remains unverified.",
                payload=canonical_object({field: item.get(field) for field in (
                    "RS_embed", "RS_llm", "RS_final", "label", "scoring_status", "llm_rationale",
                    "llm_evidence", "llm_evidence_status")}), created_at=timestamp,
            ))
            record_categories.append("evidence")
            raw_flags = item.get("quality_flags") or []
            raw_flags = [raw_flags] if isinstance(raw_flags, str) else raw_flags
            if not isinstance(raw_flags, list) or any(not isinstance(f, str) for f in raw_flags):
                raise ValueError("quality_flags must be strings")
            flags = {}
            for flag in raw_flags:
                if flag.strip():
                    flags.setdefault(category(flag), flag)
            score = number(item.get("RS_final"))
            if score is None or not 0 <= score <= 100:
                flags.setdefault("scoring_unavailable", "scoring_unavailable")
            elif score < threshold:
                flags.setdefault("low_relevance", "low_relevance")
            for name, flag in flags.items():
                priority = "high" if "retract" in name else "moderate"
                if name in {"missing_doi", "missing_abstract"}:
                    priority = "low"
                records.append(Finding(
                    id=derived_uuid(run_id, "finding", key, name), tenant_id=self.tenant_id,
                    document_version_id=document_version_id, reference_id=reference_id,
                    model_run_id=run_id, category=name, priority=priority,
                    summary=f"Automated review signal: {name.replace('_', ' ')}; human verification required.",
                    details={"legacy_flag": flag, "score": score, "requires_human_review": True},
                    policy_version="legacy-import-policy-v1", created_at=timestamp,
                ))
                record_categories.append("findings")
        created = self.repository.append_batch(records, self.actor)
        counts = {key: sum(yes and name == key for yes, name in zip(created, record_categories))
                  for key in ("references", "candidates", "evidence", "findings")}
        return LegacyCaptureReceipt(tenant_id=self.tenant_id, document_version_id=document_version_id,
                                    artifact_sha256=artifact_hash, model_run_id=run_id,
                                    references_created=counts["references"], candidate_matches_created=counts["candidates"],
                                    evidence_created=counts["evidence"], findings_created=counts["findings"])

    def verify_audit_chain(self):
        events, records = self.repository.audit_snapshot(self.tenant_id)
        return verify_audit_chain(events, tenant_id=self.tenant_id, records=records)

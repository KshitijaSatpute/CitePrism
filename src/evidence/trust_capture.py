"""Append Phase 2 observations without rewriting earlier extraction revisions."""

import hashlib
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from src.evidence import EXTRACTION_VERSION, POLICY_VERSION, SCORING_VERSION
from src.evidence.review import build_review
from src.evidence.text import digest_json, digest_text, locate_quote
from src.trust.ids import derived_uuid
from src.trust.models import ModelRun, ReferenceRecord, CandidateMatch, Evidence, Finding, SourceSpan, LegacyCaptureReceipt
from src.trust.ports import TrustIntegrityError


def capture(kernel, version, raw, artifact):
    docid, tenant = version.id, kernel.tenant_id
    source = artifact.get('source_document', {})
    if source.get('pdf_sha256') != version.content_sha256:
        raise TrustIntegrityError('Evidence artifact does not belong to this PDF version')
    revision = artifact.get('extraction_metadata', {}).get('extraction_revision')
    if not isinstance(revision, str) or len(revision) != 64:
        raise TrustIntegrityError('Extraction revision is missing')
    pages = {p['page']: p for p in source.get('pages', [])}
    for page in pages.values():
        if digest_text(page['text']) != page['text_sha256']:
            raise TrustIntegrityError('Page text checksum mismatch')
    artifact_hash = hashlib.sha256(raw).hexdigest()
    runid = derived_uuid(docid, 'evidence-import-v3', artifact_hash)
    timestamp = datetime.fromisoformat(artifact['scoring_metadata']['generated_at'])
    with tempfile.TemporaryDirectory(prefix='citeprism-evidence-') as directory:
        path = Path(directory) / 'result.json'
        path.write_bytes(raw)
        stored = kernel.object_store.put_file(path, content_sha256=artifact_hash, media_type='application/json')
    records, kinds, span_ids = [], [], {}
    def add(record, kind):
        records.append(record)
        kinds.append(kind)
    add(ModelRun(id=runid, tenant_id=tenant, document_version_id=docid, purpose='evidence_artifact_capture',
        provider='citeprism', model='deterministic-capture', prompt_version='not-applicable', method_version=SCORING_VERSION,
        input_sha256=version.content_sha256, output_sha256=artifact_hash, status='succeeded',
        started_at=timestamp, completed_at=timestamp,
        configuration={'artifact_object': stored.model_dump(mode='json'), 'extraction_revision': revision,
                       'provider_execution_observed': False}), 'runs')
    call_ids = {}
    for call in artifact.get('provider_runs', []):
        if call['input_sha256'] != digest_json(call['request']):
            raise TrustIntegrityError('Provider input checksum mismatch')
        if call['status'] == 'succeeded' and call['output_sha256'] != digest_json(call['response']):
            raise TrustIntegrityError('Provider output checksum mismatch')
        callid = derived_uuid(docid, 'provider-call', call['id'])
        if call['id'] in call_ids:
            continue
        call_ids[call['id']] = callid
        add(ModelRun(id=callid, tenant_id=tenant, document_version_id=docid, purpose=call['purpose'],
            provider=call['provider'], model=call['model'], prompt_version=call['prompt_version'],
            # Keep a recorded assessment's original version when replayed after
            # an upgrade; its immutable call ID must not acquire new metadata.
            method_version=call['prompt_version'] if call['purpose'] == 'citation_claim_assessment' else EXTRACTION_VERSION,
            input_sha256=call['input_sha256'], output_sha256=call.get('output_sha256'), status=call['status'],
            started_at=datetime.fromisoformat(call['started_at']), completed_at=datetime.fromisoformat(call['completed_at']),
            error_code=call.get('error_code'),
            configuration={'request': call['request'], 'response': call.get('response'),
                           'usage': call.get('usage'), 'attempts': call['attempts'],
                           'provider_execution_observed': call.get('provider_execution_observed', False)}), 'runs')

    def record_location(location):
        page = pages.get(location['page'])
        start, end = location['char_start'], location['char_end']
        if (page is None or not 0 <= start < end <= len(page['text']) or
                page['text_sha256'] != location['page_text_sha256'] or page['text'][start:end] != location['text']):
            raise TrustIntegrityError('Manuscript anchor does not match its page text')
        key = digest_json(location)
        if key not in span_ids:
            sid = derived_uuid(docid, 'page-text-span', key)
            add(SourceSpan(id=sid, tenant_id=tenant, document_version_id=docid, page_start=location['page'],
                page_end=location['page'], char_start=start, char_end=end, text_sha256=digest_text(location['text']),
                locator={**location, 'coordinate_system': 'extracted_page_text_characters'}, created_at=version.created_at), 'spans')
            span_ids[key] = sid
        return span_ids[key]

    reference_ids = {}
    for ordinal, ref in enumerate(artifact['scored_references'], 1):
        uid = ref['reference_uid']
        key = revision + ':' + uid
        if uid in reference_ids:
            raise TrustIntegrityError('Duplicate reference identity in evidence artifact')
        refid = derived_uuid(docid, 'reference-observation', key)
        reference_ids[uid] = refid
        original = ref['original_data']
        locations = [record_location(loc) for loc in original.get('source_locations', [])]
        add(ReferenceRecord(id=refid, tenant_id=tenant, document_version_id=docid, reference_key=key,
            ordinal=ordinal, source_span_id=locations[0] if locations else None,
            raw_text_sha256=digest_text(original.get('raw_text', '')),
            bibliographic_metadata={**original.get('parsed', {}), 'source_reference_key': ref['ref_id'],
                'source_span_ids': [str(i) for i in locations], 'extraction_revision': revision},
            created_at=version.created_at), 'references')
        selected_id = None
        for candidate in ref.get('resolution', {}).get('candidates', []):
            cid = derived_uuid(runid, 'candidate', refid, candidate['candidate_id'])
            if candidate['selected']:
                selected_id = cid
            add(CandidateMatch(id=cid, tenant_id=tenant, document_version_id=docid, reference_id=refid,
                model_run_id=runid, provider='openalex', external_id=candidate['candidate_id'],
                match_method=ref['resolution'].get('method', 'title_authors'), confidence=None,
                selected=candidate['selected'], metadata=candidate, created_at=timestamp), 'candidates')
        for occurrence in ref.get('citation_occurrences', []):
            for loc in occurrence.get('source_locations', []) + occurrence.get('context_locations', []):
                record_location(loc)
        snapshot = ref.get('source_snapshot') or {}
        if snapshot and digest_text(snapshot['text']) != snapshot['text_sha256']:
            raise TrustIntegrityError('Source snapshot checksum mismatch')
        for index, observation in enumerate(ref.get('claim_assessments', [])):
            if observation.get('status') == 'grounded_proposal':
                occurrence = next((c for c in ref.get('citation_occurrences', []) if
                                   c['occurrence_id'] == observation['occurrence_id']), None)
                anchor = observation.get('claim_anchor') or {}
                if (not occurrence or locate_quote(anchor.get('text', ''), occurrence['context_window']) != anchor or
                    not anchor['char_start'] <= occurrence['target_marker_start'] < occurrence['target_marker_end'] <= anchor['char_end'] or
                    observation.get('source_snapshot_id') != snapshot.get('snapshot_id') or
                    snapshot.get('candidate_id') != ref.get('resolution', {}).get('selected_candidate_id')):
                    raise TrustIntegrityError('Claim is not anchored to its assigned citation and source')
            mid = call_ids.get(observation.get('provider_call_id')) or runid
            add(Evidence(id=derived_uuid(runid, 'assessment', uid, index), tenant_id=tenant,
                document_version_id=docid, subject_type='reference', subject_id=refid, kind='model_rationale',
                candidate_match_id=selected_id, model_run_id=mid,
                statement='Recorded model proposal; human interpretation review remains outstanding.',
                payload=observation, created_at=timestamp), 'evidence')
            for qindex, quote in enumerate(observation.get('validated_quotes', [])):
                source = snapshot.get('text', '')
                if quote.get('located_in') == 'snapshot_abstract':
                    source = snapshot.get('abstract') or ''
                    if not source or digest_text(source) != snapshot.get('abstract_sha256'):
                        raise TrustIntegrityError('Source abstract checksum mismatch')
                located = locate_quote(quote['text'], source)
                if located is not None and quote.get('located_in'):
                    located['located_in'] = quote['located_in']
                if located != quote or observation.get('status') != 'grounded_proposal' or not selected_id:
                    raise TrustIntegrityError('Source quotation is not valid for the selected candidate')
                add(Evidence(id=derived_uuid(runid, 'source-quote', uid, index, qindex), tenant_id=tenant,
                    document_version_id=docid, subject_type='reference', subject_id=refid,
                    kind='source_quote', candidate_match_id=selected_id, model_run_id=mid,
                    statement='Quotation located in this source snapshot; interpretation is unreviewed.',
                    payload={'quote': quote, 'source_snapshot': snapshot, 'occurrence_id': observation['occurrence_id']},
                    created_at=timestamp), 'evidence')
    for finding in build_review(artifact)['findings']:
        add(Finding(id=derived_uuid(runid, 'review-finding', finding['id']), tenant_id=tenant,
            document_version_id=docid, reference_id=reference_ids.get(finding['reference_id']), model_run_id=runid,
            category=finding['category'], priority=finding['priority'], summary=finding['summary'],
            details=finding, policy_version=POLICY_VERSION, created_at=timestamp), 'findings')
    created = kernel.repository.append_batch(records, kernel.actor)
    counts = Counter(kind for kind, fresh in zip(kinds, created) if fresh)
    return LegacyCaptureReceipt(tenant_id=tenant, document_version_id=docid, artifact_sha256=artifact_hash,
        model_run_id=runid, references_created=counts['references'], candidate_matches_created=counts['candidates'],
        evidence_created=counts['evidence'], findings_created=counts['findings'])

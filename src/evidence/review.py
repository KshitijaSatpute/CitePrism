"""One versioned review projection for the UI, exports, and audit capture."""

import html
import json
from collections import Counter

from src.evidence import SCHEMA_VERSION
from src.evidence.contracts import ReviewProjection, ReviewReference, ReviewFinding
from src.evidence.text import digest_text

EXTRACTION_MESSAGES = {
    'unassigned_model_reference_observations': 'Some parsed references could not be linked to their printed bibliography labels. Reprocess the manuscript with the updated reference matcher.',
    'missing_or_duplicate_model_entry': 'A single parsed reference could not be linked to this printed entry.',
    'duplicate_printed_reference_label': 'The same reference number is printed more than once.',
    'bibliography_fragment': 'The extracted entry has no usable title; inspect its printed text.',
    'bibliography_boundary_ambiguous': 'This entry may continue into an adjacent numbered entry.',
    'title_not_grounded_in_entry': 'The model-generated title could not be located in this printed entry.',
    'authors_not_grounded_in_entry': 'Some model-generated authors could not be located in this printed entry.',
    'year_not_grounded_in_entry': 'The model-generated year could not be located in this printed entry.',
    'references_boundary_unresolved': 'The start of the bibliography could not be located.',
    'reference_entries_need_location_review': 'Reference locations need checking against the manuscript.',
}


def extraction_message(code):
    return EXTRACTION_MESSAGES.get(code, str(code).replace('_', ' ').capitalize())


def status_label(code):
    return {'extraction_needs_review': 'Bibliography needs review',
            'identity_unresolved': 'Source identity unresolved',
            'grounded_proposal': 'Quoted model proposal; awaiting review',
            'grounding_failed': 'Quotation check failed',
            'assessment_unavailable': 'Assessment unavailable',
            'provider_unavailable': 'Source provider unavailable',
            'not_assessed': 'Not assessed', 'not_found': 'No matching source found',
            'source_unavailable': 'Source text unavailable',
            'legacy_unverified': 'Earlier result; reprocessing needed'}.get(
                code, str(code).replace('_', ' ').capitalize())


def assessment_message(code):
    return {
        'invalid_response': 'The model replied, but its response format could not be read.',
        'invalid_response_schema': 'The model response did not match the required assessment format.',
        'missing_task_id': 'The model did not return an answer for this citation use.',
        'duplicate_task_id': 'The model returned multiple answers for the same citation use.',
        'claim_not_at_target_citation': 'The proposed claim could not be located at the assigned manuscript citation.',
        'quote_not_in_own_source': 'A proposed quotation could not be located in the assigned source abstract.',
        'authentication': 'The assessment provider credentials or access permissions need attention.',
        'dependency_unavailable': 'The assessment provider dependency is unavailable.',
        'rate_limit': 'The assessment provider reached its request or quota limit.',
        'service_unavailable': 'The assessment provider was temporarily unavailable.',
        'timeout': 'The assessment provider did not respond within the time limit.',
    }.get(code, 'Assessment could not be accepted; inspect its recorded response and source evidence.')


def assessment_diagnostics(data):
    """Interpretation checks are separate from a provider's successful API call."""
    return [{'reference': str(ref.get('ref_id', 'Unknown reference')),
             'task_id': item.get('task_id'), 'status': item.get('status', 'unavailable'),
             'decision': item.get('decision') if item.get('status') == 'grounded_proposal' else 'not_assessed',
             'provider_call_id': item.get('provider_call_id'), 'error_code': item.get('error_code'),
             'detail': ('Claim and source quotation checks passed; interpretation needs human review.'
                        if item.get('status') == 'grounded_proposal' else assessment_message(item.get('error_code')))}
            for ref in data.get('scored_references', []) for item in ref.get('claim_assessments', [])]


def build_review(data):
    modern = data.get('schema_version') == SCHEMA_VERSION
    references, findings = [], []
    for ordinal, ref in enumerate(data.get('scored_references', [])):
        original = ref.get('original_data', {})
        parsed = original.get('parsed', {})
        uid = ref.get('reference_uid') or digest_text(f"legacy:{ordinal}:{ref.get('ref_id')}")
        identity = ref.get('resolution', {}).get('status', 'legacy_unverified')
        evidence = ref.get('evidence_status', 'legacy_unverified')
        ref_findings = []
        def add(category, priority, summary, explanation, action, system=False):
            identifier = digest_text(uid + ':' + category)
            ref_findings.append(identifier)
            findings.append(ReviewFinding(id=identifier, reference_id=uid, category=category,
                priority=priority, summary=summary, explanation=explanation, action=action,
                source_locations=original.get('source_locations', []), system_issue=system))
        if not modern:
            add('legacy_unverified', 'informational', 'Earlier result needs reprocessing',
                'This result predates source anchoring and quote validation.', 'Run a new audit to obtain the current evidence checks.')
        else:
            if original.get('extraction_status') != 'located':
                issues = original.get('extraction_issues', ['Entry location unavailable'])
                linking_issue = 'missing_or_duplicate_model_entry' in issues
                add('bibliography_review', 'moderate',
                    'Parsed reference could not be linked' if linking_issue else 'Check the printed reference entry',
                    ' '.join(extraction_message(code) for code in issues),
                    'Reprocess with the updated reference matcher, then inspect any remaining ambiguous labels.'
                    if linking_issue else 'Compare the entry with the manuscript page; preserve the printed numbering.',
                    linking_issue)
            if identity != 'resolved':
                add('identity_unresolved', 'moderate', 'Source identity remains unresolved',
                    f'Resolution state: {status_label(identity)}. Search candidates require identity checks.',
                    'Inspect the author, title, year and DOI evidence before using a candidate abstract.',
                    identity == 'provider_unavailable')
            differences = ref.get('resolution', {}).get('metadata_differences', [])
            if differences:
                add('metadata_difference', 'moderate', 'Manuscript and source metadata differ',
                    '; '.join(f"{d['field']}: manuscript {d['as_cited']}; source {d['candidate']}" for d in differences),
                    'Check the original publication and correct the bibliography if appropriate.')
            if not parsed.get('doi'):
                add('doi_omitted_in_manuscript', 'informational', 'DOI is omitted in the manuscript',
                    'This describes the bibliography entry; it does not mean the work has no DOI.',
                    'Use a resolved source identifier when updating citation formatting.')
            if identity == 'resolved' and not ref.get('external_metadata', {}).get('doi'):
                add('identifier_unavailable', 'informational', 'No DOI returned for the resolved work',
                    'Some works have no DOI; the available metadata does not establish one.',
                    'Retain another persistent identifier or check the publication.')
            if ref.get('citation_count', 0) == 0:
                add('not_cited', 'low', 'No in-text occurrence located',
                    'The supported citation patterns did not locate a use of this entry. This is not a relevance score.',
                    'Check whether the entry is uncited or uses an unsupported citation format.')
            if evidence == 'source_unavailable':
                add('source_unavailable', 'informational', 'Source text is unavailable',
                    'Identity was resolved but an abstract could not be retrieved.',
                    'Obtain the cited source before assessing its support for the claim.')
            if evidence in {'grounding_failed', 'assessment_unavailable'}:
                codes = dict.fromkeys(a.get('error_code') for a in ref.get('claim_assessments', [])
                                      if a.get('status') != 'grounded_proposal')
                add('assessment_error', 'moderate', 'Assessment requires another attempt',
                    ' '.join(assessment_message(code) for code in codes) or assessment_message(None),
                    'Inspect the diagnostic code and rerun assessment. No support verdict was accepted.', True)
            if ref.get('external_metadata', {}).get('is_retracted') is True:
                add('retraction_reported', 'high', 'Metadata provider reports a retraction',
                    'This is a provider-reported status and needs verification against the publication notice.',
                    'Inspect the retraction notice and assess its effect on the cited claim.')
            decisions = {a.get('decision') for a in ref.get('claim_assessments', []) if a.get('status') == 'grounded_proposal'}
            if 'contradicts' in decisions:
                add('possible_contradiction', 'high', 'Model proposes a contradiction',
                    'Quoted text was located in the assigned abstract; the interpretation has not been reviewed by a person.',
                    'Read the manuscript claim beside the source and verify the interpretation.')
            if decisions & {'context_only', 'insufficient_evidence'}:
                add('limited_support', 'low', 'Available abstract does not establish the claim',
                    'The model found context or insufficient evidence. An abstract cannot establish absence of support in the full paper.',
                    'Review the full source before drawing a conclusion.')
        references.append(ReviewReference(id=uid, marker=str(ref.get('ref_id', ordinal + 1)),
            title=parsed.get('title') or ('Reference metadata unavailable' if
                'missing_or_duplicate_model_entry' in original.get('extraction_issues', [])
                else 'Incomplete bibliography entry'), identity_status=identity,
            evidence_status=evidence, citation_count=ref.get('citation_count', 0),
            claim_assessment=ref.get('claim_assessment', 'not_assessed'),
            quote_count=sum(len(a.get('validated_quotes', [])) for a in ref.get('claim_assessments', [])),
            topic_similarity=ref.get('RS_embed') if modern else None, finding_ids=ref_findings))
    counts = {'total_references': len(references), 'citation_occurrences': len(data.get('citations_in_text', [])),
        'resolved_references': sum(r.identity_status == 'resolved' for r in references),
        'unresolved_references': sum(r.identity_status != 'resolved' for r in references),
        'grounded_proposals': sum(r.evidence_status == 'grounded_proposal' for r in references),
        'unassessed_references': sum(r.evidence_status != 'grounded_proposal' for r in references),
        'references_to_review': sum(bool(r.finding_ids) for r in references),
        'system_issues': sum(f.system_issue for f in findings), 'findings': len(findings),
        'reviewer_decisions': 0}
    priorities = Counter(f.priority for f in findings)
    counts.update({f'{level}_priority_findings': priorities[level] for level in ('high', 'moderate', 'low', 'informational')})
    assessments = assessment_diagnostics(data)
    counts.update(assessment_tasks=len(assessments),
                  grounded_assessments=sum(a['status'] == 'grounded_proposal' for a in assessments),
                  unavailable_assessments=sum(a['status'] == 'unavailable' for a in assessments),
                  grounding_failures=sum(a['status'] == 'grounding_failed' for a in assessments))
    limitations = ['All assessments are unreviewed model proposals, not findings of misconduct.',
                  'Quote validation proves attribution to the supplied abstract, not that the interpretation is correct.',
                  'This release assesses retrieved abstracts; it does not search or verify full-text claims.',
                  'Citation locations refer to extracted page text. Scanned PDFs need OCR before upload.']
    if data.get('extraction_metadata', {}).get('warnings'):
        limitations.extend(extraction_message(code) for code in data['extraction_metadata']['warnings'])
    status = 'legacy_unverified' if not modern else ('completed_with_issues' if counts['unresolved_references'] or
             counts['system_issues'] or any(r.evidence_status == 'source_unavailable' for r in references) else 'completed')
    return ReviewProjection(title=data.get('manuscript_metadata', data.get('metadata', {})).get('title') or 'Untitled manuscript',
        processing_status=status, counts=counts, references=references, findings=findings, limitations=limitations).model_dump(mode='json')


def report_html(data):
    """Portable UTF-8 export using precisely the projection displayed in the app."""
    review = build_review(data)
    esc = lambda value: html.escape(str(value), quote=True)
    rows = ''.join('<tr>' + ''.join(f'<td>{esc(status_label(r[k]) if k in ("identity_status", "evidence_status", "claim_assessment") else r[k])}</td>' for k in
        ('marker', 'title', 'identity_status', 'evidence_status', 'claim_assessment')) + '</tr>' for r in review['references'])
    titles = {r['id']: r['marker'] + ' · ' + r['title'] for r in review['references']}
    findings = ''.join(f"<li><strong>{esc(titles.get(f['reference_id'], 'Manuscript'))}: {esc(f['summary'])}</strong> — "
                       f"{esc(f['explanation'])}<br>{esc(f['action'])}</li>" for f in review['findings'])
    checks = ''.join(f"<tr><td>{esc(a['reference'])}</td><td>{esc(status_label(a['status']))}</td>"
                     f"<td>{esc(status_label(a['decision']))}</td><td>{esc(a['detail'])}</td></tr>"
                     for a in assessment_diagnostics(data))
    quotes = []
    for ref in data.get('scored_references', []):
        for observation in ref.get('claim_assessments', []):
            if observation.get('status') != 'grounded_proposal':
                continue
            claim = (observation.get('claim_anchor') or {}).get('text', '')
            for quote in observation.get('validated_quotes', []):
                quotes.append(f"<article><h3>{esc(ref.get('ref_id'))}</h3><p>Manuscript: {esc(claim)}</p>"
                              f"<blockquote>{esc(quote['text'])}</blockquote><p>Source snapshot: {esc(ref['source_snapshot']['snapshot_id'])}</p></article>")
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>CitePrism evidence review</title>'
        '<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 24px;color:#182b3c;line-height:1.55}'
        'table{border-collapse:collapse;width:100%}td,th{border:1px solid #cbd5df;padding:10px;text-align:left}'
        'blockquote{border-left:3px solid #166b73;padding:16px;background:#f1f7f8}li{margin:16px 0}code{overflow-wrap:anywhere}</style>'
        f"<h1>{esc(review['title'])}</h1><p>Processing: {esc(review['processing_status'])} · Human review: not reviewed</p>"
        '<p>Abstract evidence only. Model proposals require human review.</p><table><thead><tr>'
        '<th>Reference</th><th>As cited</th><th>Identity</th><th>Evidence</th><th>Proposal</th></tr></thead>'
        f'<tbody>{rows}</tbody></table><h2>Assessment checks</h2>'
        '<p>Provider call success means a response was received. Each assessment must also pass the checks below.</p>'
        '<table><thead><tr><th>Reference</th><th>Assessment</th><th>Proposal</th><th>Detail</th></tr></thead>'
        f'<tbody>{checks}</tbody></table><h2>Review queue</h2><ul>{findings}</ul><h2>Located quotations</h2>' + ''.join(quotes) +
        '<h2>Scope and limitations</h2><ul>' + ''.join(f'<li>{esc(t)}</li>' for t in review['limitations']) + '</ul></html>')


def review_json(data):
    return json.dumps(build_review(data), ensure_ascii=False, indent=2, allow_nan=False)

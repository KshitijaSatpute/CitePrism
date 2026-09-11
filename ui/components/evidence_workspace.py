"""Interim evidence workspace shared by every Phase 2 result surface."""

import hashlib
import json
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

from src.evidence.review import (build_review, report_html, review_json, status_label,
                                 assessment_diagnostics, assessment_message)
from src.evidence.pdf_preview import render_pdf_page

_cached_pdf_page = st.cache_data(max_entries=8, show_spinner=False)(render_pdf_page)


def show_review():
    st.session_state.active_tab = 'Review'
    st.session_state.evidence_navigation = 'Review'


def _source_link(label, url):
    if isinstance(url, str) and urlparse(url).scheme == 'https':
        st.link_button(label, url)


def render_workspace(data, mode='Review', pdf_path=None):
    if not data:
        st.info('Upload a manuscript and run its audit, or choose an existing manuscript in the sidebar.')
        return
    review = build_review(data)
    counts = review['counts']
    st.header(review['title'])
    st.caption('Human review: not reviewed · Evidence scope: retrieved abstracts')
    if (data.get('scoring_metadata', {}).get('method_version') == 'grounded-assessment-v3' and
            any(a.get('error_code') == 'invalid_response' for r in data.get('scored_references', [])
                for a in r.get('claim_assessments', []))):
        st.warning('This saved result used the earlier assessment parser. Choose New audit and run the same PDF '
                   'with advanced options unchecked to refresh assessment using the current parser.')
    if (data.get('extraction_metadata', {}).get('method_version') == 'page-anchors-v3' and
            'unassigned_model_reference_observations' in data.get('extraction_metadata', {}).get('warnings', [])):
        st.warning('This saved result used the earlier reference matcher. Choose New audit and process the same PDF to rebuild its reference links.')
    if review['processing_status'] == 'legacy_unverified':
        st.warning('This is an earlier result. Run a new audit to obtain page anchors and source-specific quote checks.')
    elif review['processing_status'] == 'completed_with_issues':
        st.warning('Processing finished with unresolved items. The review queue explains what needs attention.')
    else:
        st.info('Processing finished. Model interpretations still require human review.')
    a, b, c, d = st.columns(4)
    a.metric('Reference entries', counts['total_references'])
    b.metric('Citation occurrences', counts['citation_occurrences'])
    c.metric('Resolved sources', counts['resolved_references'])
    d.metric('Grounded proposals', counts['grounded_proposals'])
    st.caption('A grounded proposal has quotations located in its assigned source. It is not a verified scientific conclusion.')
    if mode == 'Review':
        _review_queue(review)
        st.subheader('Reference inventory')
        rows = [{'Reference': r['marker'], 'As cited': r['title'], 'Source identity': status_label(r['identity_status']),
                 'Evidence': status_label(r['evidence_status']), 'Citation uses': r['citation_count'],
                 'Model proposal': status_label(r['claim_assessment'])} for r in review['references']]
        st.dataframe(rows, hide_index=True, width='stretch')
        st.subheader('Export this review')
        left, right = st.columns(2)
        left.download_button('Download review report', report_html(data).encode('utf-8'),
            file_name='CitePrism_evidence_review.html', mime='text/html', width='stretch')
        right.download_button('Download review data', review_json(data),
            file_name='CitePrism_review.json', mime='application/json', width='stretch')
        st.caption('Both exports use the same review policy and counts as this screen.')
    elif mode == 'Evidence':
        _evidence(data, review, pdf_path)
    else:
        _diagnostics(data, review)
    with st.expander('Scope and limitations'):
        for limitation in review['limitations']:
            st.write('• ' + limitation)


def _review_queue(review):
    st.subheader('Review queue')
    st.write('Start with source identity and bibliography issues, then inspect the claim evidence.')
    choice = st.selectbox('Show', ['Actionable findings', 'System issues', 'All findings'])
    findings = review['findings']
    if choice == 'Actionable findings':
        findings = [f for f in findings if f['priority'] != 'informational']
    elif choice == 'System issues':
        findings = [f for f in findings if f['system_issue']]
    order = {'high': 0, 'moderate': 1, 'low': 2, 'informational': 3}
    titles = {r['id']: f"{r['marker']} · {r['title']}" for r in review['references']}
    for finding in sorted(findings, key=lambda f: order[f['priority']]):
        title = titles.get(finding['reference_id'], 'Manuscript')
        with st.expander(f"{finding['priority'].title()} · {finding['summary']} · {title}"):
            st.write(finding['explanation'])
            st.write('**Next action:** ' + finding['action'])
            pages = sorted({loc['page'] for loc in finding['source_locations']})
            if pages:
                st.caption('Manuscript page(s): ' + ', '.join(map(str, pages)))
            def select_evidence(uid=finding['reference_id']):
                st.session_state.selected_evidence_reference = uid
                st.session_state.active_tab = 'Evidence'
                st.session_state.evidence_navigation = 'Evidence'
            st.button('Inspect evidence', key='finding-' + finding['id'], on_click=select_evidence)
    if not findings:
        st.info('No findings match this filter. This does not establish that all citations are correct.')


def _evidence(data, review, pdf_path):
    rows = data.get('scored_references', [])
    if not rows:
        st.info('No reference entries are available. Inspect the extraction diagnostics.')
        return
    ids = [r['id'] for r in review['references']]
    labels = {r['id']: r['marker'] + ' · ' + r['title'] for r in review['references']}
    requested = st.session_state.get('selected_evidence_reference')
    if requested not in ids:
        st.session_state.selected_evidence_reference = ids[0]
    selected = st.selectbox('Reference to inspect', ids, format_func=labels.get, key='selected_evidence_reference')
    ref = rows[ids.index(selected)]
    original = ref.get('original_data', {})
    left, right = st.columns(2, gap='large')
    with left:
        st.subheader('Manuscript evidence')
        st.write('**As cited**')
        st.text(original.get('raw_text') or 'Printed entry was not located in this result.')
        st.json(original.get('parsed', {}), expanded=False)
        for occurrence in ref.get('citation_occurrences', []):
            pages = sorted({loc['page'] for loc in occurrence.get('source_locations', [])})
            st.write(f"**{occurrence['marker']} · page {', '.join(map(str, pages))}**")
            st.text(occurrence['context_window'])
        with st.expander('Open manuscript PDF'):
            _manuscript_preview(data, original, pdf_path)
    with right:
        st.subheader('Source evidence')
        resolution = ref.get('resolution', {})
        st.write('**Source identity:** ' + status_label(resolution.get('status', 'legacy_unverified')))
        external = ref.get('external_metadata', {})
        snapshot = ref.get('source_snapshot') or {}
        if snapshot:
            st.write(external.get('title', 'Resolved source'))
            st.text(snapshot['text'])
            st.caption(f"{snapshot['provider']} · {snapshot['kind']} · retrieved {snapshot['retrieved_at']}")
            _source_link('Open source record', snapshot.get('url'))
        else:
            st.info('No source text is eligible for claim assessment.')
        with st.expander('Candidate identity evidence'):
            st.caption('Candidates are search alternatives. Rank strength is a rule-based ranking, not a probability.')
            st.json(resolution)
        for assessment in ref.get('claim_assessments', []):
            st.write('**Assessment:** ' + assessment['status'].replace('_', ' '))
            if assessment['status'] == 'grounded_proposal':
                st.write('**Unreviewed proposal:** ' + assessment['decision'].replace('_', ' '))
                st.write(assessment.get('rationale', ''))
                for quote in assessment.get('validated_quotes', []):
                    st.text('“' + quote['text'] + '”')
                st.caption('Quotation attribution checked. Interpretation requires human review.')
            else:
                st.warning(assessment_message(assessment.get('error_code')))
                st.caption('No support verdict accepted. Diagnostic: ' + assessment.get('error_code', 'unavailable'))


def _manuscript_preview(data, original, pdf_path):
    if not pdf_path or not Path(pdf_path).is_file():
        st.info('The original PDF is not available in this workspace.')
        return
    raw = Path(pdf_path).read_bytes()
    expected_hash = data.get('source_document', {}).get('pdf_sha256')
    if expected_hash and hashlib.sha256(raw).hexdigest() != expected_hash:
        st.error('This stored PDF differs from the audited manuscript. Upload the original PDF again.')
        return
    st.download_button('Download manuscript PDF', raw, 'manuscript.pdf', 'application/pdf')
    try:
        from pypdf import PdfReader
        count = len(PdfReader(BytesIO(raw)).pages)
        initial_page = next(iter(original.get('source_locations', [])), {}).get('page', 1)
        selected_page = st.selectbox('Manuscript page', range(1, count + 1),
            index=min(max(initial_page - 1, 0), count - 1),
            key='pdf-page-' + str(original.get('reference_uid', 'manuscript')))
        png, count = _cached_pdf_page(raw, selected_page)
        st.image(png, caption=f'Manuscript page {selected_page} of {count}', width='stretch')
        st.caption('Page preview. Citation locations are also available in the extracted text above.')
    except ImportError:
        st.info('The page preview dependency is missing. Install requirements-phase2.txt; the PDF download remains available.')
    except Exception:
        st.info('This page could not be rendered. Download the PDF to inspect the original document.')


def _diagnostics(data, review):
    st.subheader('Processing diagnostics')
    counts = review['counts']
    first, second, third = st.columns(3)
    first.metric('Citation uses submitted', counts['assessment_tasks'])
    second.metric('Passed quotation checks', counts['grounded_assessments'])
    third.metric('Assessments needing attention', counts['unavailable_assessments'] + counts['grounding_failures'])
    st.caption('Passing quotation checks establishes attribution. A reviewer still needs to assess the interpretation.')
    assessments = assessment_diagnostics(data)
    if assessments:
        st.dataframe([{'Reference': a['reference'], 'Assessment': status_label(a['status']),
            'Model proposal': status_label(a['decision']), 'Detail': a['detail']}
            for a in assessments], hide_index=True, width='stretch')
    else:
        st.info('No citation use reached assessment. Check source identity, citation locations and abstract availability.')
    for title, field in [('Extraction', 'extraction_metadata'), ('Resolution', 'enrichment_metadata'), ('Assessment', 'scoring_metadata')]:
        with st.expander(title):
            st.json(data.get(field, {}))
    runs = data.get('provider_runs', [])
    st.caption('API call success means a response was received. Assessment acceptance is reported separately above.')
    st.dataframe([{'Provider': r['provider'], 'Model': r['model'], 'Purpose': r['purpose'],
        'API call': r['status'], 'Attempts': len(r['attempts']), 'Seconds': r.get('elapsed_seconds'),
        'Error': r.get('error_code')} for r in runs], hide_index=True, width='stretch')
    st.download_button('Download complete evidence artifact', json.dumps(data, indent=2, ensure_ascii=False),
                       'CitePrism_evidence_artifact.json', 'application/json')
    st.caption('The complete artifact includes manuscript text, source snapshots, and recorded provider prompts/responses.')
    st.info('Agreement metrics need independently reviewed labels. Passing regression tests does not measure scientific accuracy.')

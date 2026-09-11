"""Replay the user's complete assessment response without any provider calls."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.evidence import SCORING_VERSION
from src.evidence.assessment import decode_assessments, AssessmentResponseError
from src.evidence.review import assessment_diagnostics, build_review, report_html
from src.evidence.text import digest_json, locate_quote, locate_manuscript_quote
from src.scorers.relevance_scorer import HFBatchScorer, RelevanceScorer
from tests.trust_fixtures import make_kernel
from src.trust.memory_repository import InMemoryTrustRepository

FIXTURE = Path(__file__).parent / 'fixtures/evidence/assessment_list_response.json'
PDF = FIXTURE.parent / 'underwater_manuscript.pdf'


def fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class SavedRecorder:
    """Return recorded bytes; never invoke a client or claim a fresh execution."""
    def __init__(self, call, response=None):
        self.call_data = copy.deepcopy(call)
        if response is not None:
            self.call_data['response'] = response
            self.call_data['output_sha256'] = digest_json(response)
        self.records = []

    def call(self, **kwargs):
        if kwargs['request']['tasks'] != self.call_data['request']['tasks']:
            raise AssertionError('Replay tasks differ from the recorded request')
        self.records.append(copy.deepcopy(self.call_data))
        return self.call_data['response']


def replay(root, response=None):
    value = fixture()
    provider = object.__new__(HFBatchScorer)
    provider.model = value['assessment_call']['model']
    provider.recorder = SavedRecorder(value['assessment_call'], response)
    # No client is installed on this object: a network invocation would fail.
    scorer = RelevanceScorer(SimpleNamespace(DATA_DIR=root, HF_MODEL=provider.model, HF_BATCH_SIZE=5), None)
    scorer.llm_scorer = provider
    result = scorer.score_references(value['input'])
    result['replay'] = {'mode': 'recorded_response_revalidation', 'live_provider_calls': False}
    return result


class AssessmentResponseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_recorded_list_and_named_envelope_have_identical_rows(self):
        raw = fixture()['assessment_call']['response']
        rows = decode_assessments(raw)
        self.assertEqual(len(rows), 3)
        self.assertEqual(decode_assessments(json.dumps({'assessments': rows})), rows)
        self.assertEqual(decode_assessments('```json\n'+raw+'\n```'), rows)
        self.assertEqual(rows, json.loads(raw))

    def test_malformed_and_ambiguous_envelopes_are_rejected(self):
        for raw in ['[{', 'null', '{"items": []}', '[null]', '{"assessments": {}}',
                    '{"assessments": [NaN]}', '[] trailing prose']:
            with self.subTest(raw=raw), self.assertRaises(AssessmentResponseError):
                decode_assessments(raw)

    def test_saved_answers_produce_three_anchored_proposals_and_keep_identity_states(self):
        original = fixture()
        result = replay(self.root)
        refs = {r['ref_id']: r for r in result['scored_references']}
        self.assertEqual(result['scoring_metadata']['method_version'], SCORING_VERSION)
        self.assertEqual(result['review']['counts']['resolved_references'], 4)
        self.assertEqual(result['review']['counts']['grounded_proposals'], 3)
        self.assertEqual(result['review']['counts']['grounded_assessments'], 3)
        self.assertEqual(result['review']['counts']['system_issues'], 0)
        self.assertEqual(result['scoring_metadata']['diagnostics'], [])
        for marker, verdict in [('[3]', 'insufficient_evidence'), ('[4]', 'context_only'), ('[5]', 'insufficient_evidence')]:
            ref = refs[marker]
            self.assertEqual(ref['claim_assessment'], verdict)
            self.assertIsNone(ref['RS_final'])
            assessment = ref['claim_assessments'][0]
            context = ref['citation_occurrences'][0]['context_window']
            anchor = assessment['claim_anchor']
            self.assertEqual(context[anchor['char_start']:anchor['char_end']], anchor['text'])
            self.assertEqual(locate_quote(anchor['text'], context), anchor)
        self.assertIn('real-\ntime', refs['[4]']['claim_assessments'][0]['claim_anchor']['text'])
        self.assertEqual(refs['[7]']['evidence_status'], 'not_cited')
        self.assertEqual([r['resolution'] for r in result['scored_references']],
                         [r['resolution'] for r in original['input']['enriched_references']])
        self.assertEqual(result['provider_runs'][0], original['assessment_call'])

    def test_pdf_line_wrap_keeps_original_offsets_without_changing_words(self):
        for newline in ['\n', '\r\n']:
            text = 'A real-'+newline+'time audio experiment uses a red LED [4].'
            quote = 'A real-time audio experiment uses a red LED [4].'
            self.assertIsNone(locate_quote(quote, text))
            found = locate_manuscript_quote(quote, text)
            self.assertEqual(found['text'], text)
            self.assertEqual((found['char_start'], found['char_end']), (0, len(text)))
            self.assertIsNone(locate_manuscript_quote(quote.replace('red', 'blue'), text))
            self.assertIsNone(locate_manuscript_quote(quote.replace('real-time', 'realtime'), text))
        self.assertIsNone(locate_manuscript_quote(quote, 'A real- time audio experiment uses a red LED [4].'))

    def test_duplicate_foreign_ids_and_foreign_quotes_still_fail(self):
        original_rows = decode_assessments(fixture()['assessment_call']['response'])
        variants = []
        duplicate = copy.deepcopy(original_rows); duplicate.append(copy.deepcopy(duplicate[0]))
        variants.append((duplicate, 'duplicate_task_id'))
        foreign_id = copy.deepcopy(original_rows); foreign_id[0]['task_id'] = 'foreign-task'
        variants.append((foreign_id, 'missing_task_id'))
        foreign_quote = copy.deepcopy(original_rows)
        foreign_quote[0]['source_quotes'] = ['An invented source quotation cannot establish support for any cited claim.']
        variants.append((foreign_quote, 'quote_not_in_own_source'))
        wrong_claim = copy.deepcopy(original_rows)
        wrong_claim[0]['claim_quote'] = fixture()['assessment_call']['request']['tasks'][0]['manuscript_context'][:70]
        variants.append((wrong_claim, 'claim_not_at_target_citation'))
        for rows, code in variants:
            with self.subTest(code=code):
                result = replay(self.root, json.dumps(rows))
                first = result['scored_references'][2]['claim_assessments'][0]
                self.assertEqual(first['error_code'], code)
                self.assertNotEqual(first['status'], 'grounded_proposal')
                self.assertIn(code, [d['code'] for d in result['scoring_metadata']['diagnostics']])

    def test_successful_call_with_invalid_content_is_visible_as_failed_assessment(self):
        result = replay(self.root, '{"assessments": "bad shape"}')
        self.assertEqual(result['provider_runs'][0]['status'], 'succeeded')
        checks = assessment_diagnostics(result)
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(a['status'] == 'unavailable' for a in checks))
        self.assertTrue(all(a['error_code'] == 'invalid_response_schema' for a in checks))
        self.assertEqual(result['review']['counts']['unavailable_assessments'], 3)
        self.assertEqual(len(result['scoring_metadata']['diagnostics']), 3)
        self.assertIn('required assessment format', report_html(result))

    def test_replay_capture_keeps_old_run_and_validates_new_anchors(self):
        from unittest.mock import patch
        kernel = make_kernel(self.root, InMemoryTrustRepository())
        receipt = kernel.ingest_pdf(PDF)
        data = replay(self.root)
        old = copy.deepcopy(data)
        old['scoring_metadata']['method_version'] = 'grounded-assessment-v3'
        for ref in old['scored_references']:
            for observation in ref['claim_assessments']:
                observation.update(status='unavailable', decision='insufficient_evidence',
                                   validated_quotes=[], claim_anchor=None, error_code='invalid_response')
            if ref['claim_assessments']:
                ref.update(evidence_status='assessment_unavailable', claim_assessment='not_assessed')
        path = self.root / 'replay.json'
        path.write_text(json.dumps(old), encoding='utf-8')
        with patch('src.evidence.trust_capture.SCORING_VERSION', 'grounded-assessment-v3'):
            kernel.capture_legacy_scored_artifact(receipt.document_version_id, path)
        path.write_text(json.dumps(data), encoding='utf-8')
        first = kernel.capture_legacy_scored_artifact(receipt.document_version_id, path)
        self.assertEqual(first.references_created, 0)
        self.assertGreater(first.evidence_created, 0)
        self.assertEqual(kernel.capture_legacy_scored_artifact(receipt.document_version_id, path).evidence_created, 0)
        self.assertTrue(kernel.verify_audit_chain().valid)

    def test_report_identifies_references_and_keeps_model_proposals_unreviewed(self):
        data = replay(self.root)
        report = report_html(data)
        self.assertIn('[3] · 5 Mbps', report)
        self.assertIn('Assessment checks', report)
        self.assertIn('Human review: not reviewed', report)
        self.assertEqual(build_review(data)['counts']['reviewer_decisions'], 0)


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit is not installed')
class AssessmentDiagnosticsUiTests(unittest.TestCase):
    def test_ui_separates_successful_calls_from_rejected_answers(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as directory:
            data = replay(Path(directory), '{"assessments": "bad shape"}')
        app = AppTest.from_string('''
import streamlit as st
from ui.components.evidence_workspace import render_workspace
render_workspace(st.session_state.artifact, 'Diagnostics')
''')
        app.session_state.artifact = data
        app.run(timeout=15)
        self.assertFalse(app.exception)
        metrics = {item.label: item.value for item in app.metric}
        self.assertEqual(metrics['Citation uses submitted'], '3')
        self.assertEqual(metrics['Passed quotation checks'], '0')
        self.assertEqual(metrics['Assessments needing attention'], '3')
        self.assertTrue(any('API call success means a response was received' in x.value for x in app.caption))

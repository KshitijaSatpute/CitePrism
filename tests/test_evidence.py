import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.evidence.extraction import anchor_manuscript
from src.evidence.identity import resolve_candidates
from src.evidence.providers import CallRecorder, ProviderFailure, strict_json
from src.evidence.review import build_review, report_html, review_json
from src.evidence.text import digest_json, digest_text, locate_quote
from src.trust.memory_repository import InMemoryTrustRepository
from src.trust.ports import TrustIntegrityError
from tests.evidence_fixtures import PDF, parsed, legacy, enriched, scored, GroundedProvider, work_from_external
from tests.trust_fixtures import make_kernel


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_manuscript_has_six_occurrences_and_ten_preserved_slots(self):
        result = parsed()
        self.assertEqual([c['marker'] for c in result['citations_in_text']], ['[1]', '[9]', '[4]', '[3]', '[5]', '[2]'])
        self.assertEqual(len({c['occurrence_id'] for c in result['citations_in_text']}), 6)
        self.assertEqual(len(result['references_list']), 10)
        self.assertEqual(result['extraction_metadata']['bibliography_ambiguities'][0]['reference_ids'], ['[8]', '[9]'])
        pages = {p['page']: p['text'] for p in result['source_document']['pages']}
        for item in result['citations_in_text'] + result['references_list']:
            for loc in item['source_locations']:
                self.assertEqual(pages[loc['page']][loc['char_start']:loc['char_end']], loc['text'])

    def test_same_context_at_two_locations_is_not_deduplicated(self):
        text = 'A sufficiently long claim cites [1].\n\nA sufficiently long claim cites [1].\n\n9. References\n[1] Smith. 2020. A title.'
        source = {'pdf_sha256': 'a'*64, 'pages': [{'page': 1, 'text': text, 'text_sha256': digest_text(text)}]}
        result = anchor_manuscript({'metadata': {}, 'references_list': [{'ref_id': '[1]', 'parsed': {'title': 'A title'}}]}, source)
        self.assertEqual(len(result['citations_in_text']), 2)
        self.assertNotEqual(*[c['occurrence_id'] for c in result['citations_in_text']])

    def test_wrong_author_candidates_from_uploaded_result_are_rejected(self):
        for index in (0, 8):
            old = legacy()['scored_references'][index]
            resolution = resolve_candidates([work_from_external(old['external_metadata'])], old['original_data']['parsed'])
            self.assertEqual(resolution['status'], 'unresolved')
            self.assertIn('authors_conflict', resolution['candidates'][0]['conflicts'])

    def test_correct_authors_and_title_retain_year_difference(self):
        old = legacy()['scored_references'][2]
        resolution = resolve_candidates([work_from_external(old['external_metadata'])], old['original_data']['parsed'])
        self.assertEqual(resolution['status'], 'resolved')
        self.assertEqual(resolution['metadata_differences'][0]['field'], 'year')

    def test_tied_sources_abstain_and_conflicting_doi_does_not_override_authors(self):
        metadata = {'title': 'A specific reference title', 'authors': ['Smith, J.'], 'year': 2020}
        first = {'id': 'W1', 'title': metadata['title'], 'authors': ['John Smith'], 'year': 2020}
        second = {**first, 'id': 'W2'}
        self.assertEqual(resolve_candidates([first, second], metadata)['status'], 'ambiguous')
        metadata['doi'] = '10.1000/example'
        candidate = {**first, 'doi': metadata['doi'], 'authors': ['Other Person']}
        self.assertEqual(resolve_candidates([candidate], metadata)['status'], 'unresolved')

    def test_enrichment_never_drops_empty_or_ambiguous_entries(self):
        data = enriched(self.root)
        self.assertEqual(len(data['enriched_references']), 10)
        refs = {r['ref_id']: r for r in data['enriched_references']}
        self.assertEqual(refs['[8]']['resolution']['status'], 'extraction_needs_review')
        self.assertEqual(refs['[9]']['resolution']['status'], 'extraction_needs_review')
        self.assertIsNone(refs['[9]']['source_snapshot'])

    def test_quote_normalization_only_collapses_whitespace(self):
        source = 'This is a long source\nquotation with measured results.'
        self.assertIsNotNone(locate_quote('This is a long source quotation with measured results.', source))
        self.assertIsNone(locate_quote('This is a long source quotation with invented results.', source))
        self.assertIsNone(locate_quote('This', source))

    def test_foreign_source_quote_is_rejected_without_support_score(self):
        class Foreign(GroundedProvider):
            def judge_batch(self, tasks):
                rows = super().judge_batch(tasks)
                for row in rows:
                    row['decision'] = 'supports'
                    row['source_quotes'] = ['A quotation that was never present in any of these assigned source abstracts.']
                return rows
        data = scored(self.root, Foreign())
        attempted = [r for r in data['scored_references'] if r['claim_assessments']]
        self.assertTrue(attempted)
        for ref in attempted:
            self.assertEqual(ref['evidence_status'], 'grounding_failed')
            # An ungrounded answer contributes nothing to relevance. Topic
            # similarity may still exist, but it can never yield a support or
            # irrelevance verdict on its own.
            self.assertIsNone(ref['RS_llm'])
            self.assertIn(ref['relevance']['basis'], {'embedding_only', 'none'})
            self.assertNotEqual(ref['relevance']['label'], 'potentially_irrelevant')
            self.assertEqual(ref['llm_evidence'], [])

    def test_duplicate_and_missing_task_ids_are_not_counted_as_success(self):
        class Duplicate(GroundedProvider):
            def judge_batch(self, tasks):
                row = super().judge_batch(tasks)[0]
                return [row, copy.deepcopy(row)]
        data = scored(self.root, Duplicate())
        codes = {a.get('error_code') for r in data['scored_references'] for a in r['claim_assessments']}
        self.assertIn('duplicate_task_id', codes)
        self.assertIn('missing_task_id', codes)
        self.assertEqual(data['review']['counts']['grounded_proposals'], 0)

    def test_unavailable_and_uncited_are_not_irrelevant(self):
        data = scored(self.root)
        refs = {r['ref_id']: r for r in data['scored_references']}
        self.assertEqual(refs['[6]']['citation_count'], 0)
        self.assertIsNone(refs['[6]']['RS_final'])
        self.assertEqual(refs['[7]']['evidence_status'], 'not_cited')
        self.assertEqual(data['review']['counts']['total_references'], 10)
        self.assertEqual(data['review']['counts']['citation_occurrences'], 6)
        self.assertEqual(data['review']['counts']['reviewer_decisions'], 0)

    def test_shared_projection_and_export_escape_untrusted_text(self):
        data = scored(self.root)
        data['manuscript_metadata']['title'] = '<script>alert(1)</script>'
        self.assertEqual(json.loads(review_json(data)), build_review(data))
        report = report_html(data)
        self.assertNotIn('<script>', report)
        self.assertIn('&lt;script&gt;', report)
        self.assertIn('Human review: not reviewed', report)
        self.assertEqual(build_review(legacy())['processing_status'], 'legacy_unverified')

    def test_authentication_error_is_not_retried_and_does_not_leak_secret(self):
        recorder = CallRecorder(self.root, retry_delay=0)
        calls = []
        def invoke():
            calls.append(True)
            raise ValueError('API key not valid: secret-do-not-record')
        with self.assertRaisesRegex(ProviderFailure, 'Check the configured API key'):
            recorder.call(provider='google', model='test', purpose='test', prompt_version='v1',
                          request={'prompt': 'document'}, invoke=invoke, decode=lambda x: x)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('secret-do-not-record', json.dumps(recorder.records))
        self.assertEqual(len(list(self.root.glob('*.json'))), 1)

    def test_transient_retry_is_bounded_and_exact_output_is_recorded(self):
        recorder = CallRecorder(self.root, retry_delay=0)
        calls = []
        def invoke():
            calls.append(True)
            if len(calls) < 3:
                raise TimeoutError('temporary')
            return '{"complete": true}'
        output = recorder.call(provider='test', model='test', purpose='test', prompt_version='v1',
                               request={'prompt': 'document'}, invoke=invoke, decode=lambda x: (x, {'tokens': 3}))
        self.assertEqual(recorder.records[0]['output_sha256'], digest_json(output))
        self.assertEqual(len(calls), 3)
        with self.assertRaises(ValueError):
            strict_json('{"references": [')

    def test_reprocessing_adds_observations_without_conflicting_with_prior_revision(self):
        repo = InMemoryTrustRepository()
        kernel = make_kernel(self.root, repo)
        receipt = kernel.ingest_pdf(PDF)
        data = scored(self.root)
        path = self.root / 'result.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        first = kernel.capture_legacy_scored_artifact(receipt.document_version_id, path)
        repeated = kernel.capture_legacy_scored_artifact(receipt.document_version_id, path)
        self.assertEqual(first.references_created, 10)
        self.assertEqual(repeated.references_created, 0)
        data['extraction_metadata']['extraction_revision'] = 'b'*64
        data['scored_references'][0]['original_data']['parsed']['year'] = 2023
        path.write_text(json.dumps(data), encoding='utf-8')
        changed = kernel.capture_legacy_scored_artifact(receipt.document_version_id, path)
        self.assertEqual(changed.references_created, 10)
        self.assertTrue(kernel.verify_audit_chain().valid)

    def test_capture_rejects_cross_document_and_corrupt_page_anchors(self):
        kernel = make_kernel(self.root, InMemoryTrustRepository())
        receipt = kernel.ingest_pdf(PDF)
        data = scored(self.root)
        data['scored_references'][0]['original_data']['source_locations'][0]['text'] = 'changed'
        path = self.root / 'result.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(TrustIntegrityError):
            kernel.capture_legacy_scored_artifact(receipt.document_version_id, path)
        data = scored(self.root)
        data['source_document']['pdf_sha256'] = '0'*64
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(TrustIntegrityError):
            kernel.capture_legacy_scored_artifact(receipt.document_version_id, path)

    def test_changed_configuration_or_legacy_output_invalidates_cache(self):
        from pipeline_orchestrator import current_artifact, configuration_fingerprint
        from src.evidence import SCHEMA_VERSION, SCORING_VERSION
        config = SimpleNamespace(HF_MODEL='first')
        fingerprint = configuration_fingerprint(config, 'scoring')
        path = self.root / 'cache.json'
        path.write_text(json.dumps({'schema_version': SCHEMA_VERSION,
            'scoring_metadata': {'method_version': SCORING_VERSION, 'configuration_fingerprint': fingerprint}}))
        self.assertTrue(current_artifact(path, 'scoring_metadata', SCORING_VERSION, fingerprint))
        config.HF_MODEL = 'second'
        self.assertFalse(current_artifact(path, 'scoring_metadata', SCORING_VERSION, configuration_fingerprint(config, 'scoring')))
        self.assertFalse(current_artifact(None, 'scoring_metadata', SCORING_VERSION, fingerprint))

    def test_full_pipeline_uses_current_cache_and_refreshes_downstream_after_reparse(self):
        from unittest.mock import patch
        from database_manager import DatabaseManager
        from pipeline_orchestrator import PipelineOrchestrator
        from src.extractors.gemini_extractor import GeminiExtractor
        from src.enrichers.openalex_enricher import OpenAlexEnricher
        config = SimpleNamespace(DATA_DIR=self.root, RAW_PDFS_DIR=self.root/'raw',
            PARSED_DIR=self.root/'parsed', ENRICHED_DIR=self.root/'enriched', SCORED_DIR=self.root/'scored',
            MAX_PDF_BYTES=2*1024**2, HF_BATCH_SIZE=5, LLM_PROVIDER='google', GOOGLE_MODEL='fixture-model',
            ENABLE_GAP_ANALYSIS=False)
        db = DatabaseManager(self.root/'workflow.db')
        kernel = make_kernel(self.root, InMemoryTrustRepository())
        pipeline = PipelineOrchestrator(db, config, trust_kernel=kernel)
        enriched_fixture = enriched(self.root)
        with patch.object(GeminiExtractor, 'extract', side_effect=lambda *a, **k: parsed()) as extract, \
             patch.object(OpenAlexEnricher, 'enrich_references', side_effect=lambda *a, **k: copy.deepcopy(enriched_fixture)) as enrich, \
             patch('src.scorers.relevance_scorer.HFBatchScorer', return_value=GroundedProvider()):
            first = pipeline.process_document(PDF, force_score=False, original_filename='Underwater manuscript.pdf')
            self.assertTrue(first['success'], first['errors'])
            self.assertIn('trust_capture', first['stages_completed'])
            second = pipeline.process_document(PDF, force_score=False)
            self.assertTrue(second['success'], second['errors'])
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(enrich.call_count, 1)
            self.assertIn('scoring', second['stages_skipped'])
            third = pipeline.process_document(PDF, force_parse=True, force_score=False)
            self.assertTrue(third['success'], third['errors'])
            self.assertEqual(extract.call_count, 2)
            self.assertEqual(enrich.call_count, 2)
            self.assertIn('scoring', third['stages_completed'])
            self.assertTrue(kernel.verify_audit_chain().valid)

    def test_cached_resolution_retains_original_query_records(self):
        from src.enrichers.openalex_enricher import OpenAlexEnricher
        from tests.evidence_fixtures import Cache
        from unittest.mock import patch
        cache = Cache()
        enricher = OpenAlexEnricher(SimpleNamespace(DATA_DIR=self.root), cache)
        value = parsed()
        value['references_list'] = value['references_list'][2:3]
        old = legacy()['scored_references'][2]
        work = work_from_external(old['external_metadata'])
        response = {'results': [work]}
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return response
        with patch.object(enricher.session, 'get', return_value=Response()) as get:
            first = enricher.enrich_references(value)
            second = enricher.enrich_references(value)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(first['provider_runs'], second['provider_runs'])
        self.assertEqual(second['enrichment_summary']['cache_hits'], 1)

    def test_parser_uses_two_metadata_calls_and_leaves_citations_to_pdf_locations(self):
        from src.extractors.gemini_extractor import GoogleInterface
        from src.evidence.extraction import joined_source
        from unittest.mock import Mock
        value = parsed()
        text, _ = joined_source(value['source_document'])
        interface = object.__new__(GoogleInterface)
        interface._call_api = Mock(side_effect=[json.dumps({'metadata': value['metadata']}),
                                               json.dumps({'references_list': value['references_list']})])
        result = interface.parse_manuscript(text)
        self.assertEqual(interface._call_api.call_count, 2)
        self.assertEqual(result['citations_in_text'], [])
        anchored = anchor_manuscript(result, value['source_document'])
        self.assertEqual(len(anchored['citations_in_text']), 6)
        self.assertEqual(len(anchored['references_list']), 10)
        self.assertNotIn('8) Abdelrahman', interface._call_api.call_args_list[0].args[0])
        self.assertIn('8) Abdelrahman', interface._call_api.call_args_list[1].args[0])

    def test_unlocatable_bibliography_does_not_count_reference_markers_as_citations(self):
        text = 'A body paragraph.\n[1] Smith. A reference without a section heading.'
        source = {'pdf_sha256': 'c'*64, 'pages': [{'page': 1, 'text': text, 'text_sha256': digest_text(text)}]}
        value = anchor_manuscript({'metadata': {}, 'references_list': [{'ref_id': '[1]', 'parsed': {}}]}, source)
        self.assertEqual(value['citations_in_text'], [])
        self.assertIn('references_boundary_unresolved', value['extraction_metadata']['warnings'])
        self.assertEqual(len(value['references_list']), 1)

    def test_quote_from_neighboring_claim_does_not_validate_target_occurrence(self):
        class WrongClaim(GroundedProvider):
            def judge_batch(self, tasks):
                rows = super().judge_batch(tasks)
                for row, task in zip(rows, tasks):
                    row['claim_quote'] = task['manuscript_context'][:60]
                return rows
        data = scored(self.root, WrongClaim())
        self.assertEqual(data['review']['counts']['grounded_proposals'], 0)
        codes = {a.get('error_code') for r in data['scored_references'] for a in r['claim_assessments']}
        self.assertIn('claim_not_at_target_citation', codes)

    def test_valid_own_source_quotes_produce_grounded_proposals_with_target_anchors(self):
        data = scored(self.root)
        grounded = [r for r in data['scored_references'] if r['evidence_status'] == 'grounded_proposal']
        self.assertEqual({r['ref_id'] for r in grounded}, {'[3]', '[4]', '[5]'})
        for ref in grounded:
            self.assertTrue(ref['claim_assessments'][0]['validated_quotes'])
            self.assertIn(ref['ref_id'], ref['claim_assessments'][0]['claim_anchor']['text'])

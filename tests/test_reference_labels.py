"""Actual 1)/2) response regression plus page preview checks, without model calls."""

import copy
import hashlib
import importlib.util
import json
import unittest
from io import BytesIO
from pathlib import Path

from src.evidence import EXTRACTION_VERSION
from src.evidence.extraction import anchor_manuscript, pdf_source
from src.evidence.review import build_review, extraction_message, report_html
from src.evidence.text import canonical_id, digest_text, marker_ids
from src.extractors.gemini_extractor import ManuscriptStructure

FIXTURES = Path(__file__).parent / 'fixtures' / 'evidence'
PDF = FIXTURES / 'underwater_manuscript.pdf'


def recorded_observations():
    fixture = json.loads((FIXTURES / 'numbered_label_response.json').read_text(encoding='utf-8'))
    value = json.loads(fixture['metadata_response'])
    value.update(json.loads(fixture['bibliography_response']))
    return fixture, ManuscriptStructure.model_validate(value).model_dump()


def source_with_entries(entries):
    text = 'A claim uses [1].\n\nReferences\n' + entries
    return {'pdf_sha256': 'a' * 64, 'pages': [
        {'page': 1, 'text': text, 'text_sha256': digest_text(text)}]}


class ReferenceLabelTests(unittest.TestCase):
    def test_actual_response_links_ten_entries_and_keeps_two_for_review(self):
        fixture, value = recorded_observations()
        self.assertEqual(hashlib.sha256(PDF.read_bytes()).hexdigest(), fixture['pdf_sha256'])
        self.assertEqual([r['ref_id'] for r in value['references_list']], [f'{i})' for i in range(1, 11)])
        result = anchor_manuscript(value, pdf_source(PDF))
        self.assertEqual(len(result['references_list']), 10)
        self.assertEqual(len(result['citations_in_text']), 6)
        self.assertTrue(all(r['parsed'] for r in result['references_list']))
        self.assertEqual(result['extraction_metadata']['linked_model_entries'], 10)
        self.assertEqual(result['extraction_metadata']['unassigned_model_entries'], 0)
        self.assertNotIn('unassigned_model_references', result)
        pending = [r['ref_id'] for r in result['references_list'] if r['extraction_status'] != 'located']
        self.assertEqual(pending, ['[8]', '[9]'])
        self.assertEqual(result['references_list'][0]['model_ref_id'], '1)')
        self.assertTrue(result['references_list'][0]['raw_text'].startswith('1) Srikanth'))
        self.assertEqual(result['references_list'][0]['parsed']['title'], 'Li -Fi Based Underwater Communication')
        self.assertNotEqual(EXTRACTION_VERSION, 'page-anchors-v3')
        self.assertEqual(result['extraction_metadata']['method_version'], EXTRACTION_VERSION)

    def test_supported_numeric_labels_link_across_printed_and_model_formats(self):
        variants = ['1', '[1]', '1)', '1.', '(1)', ' [ 1 ] ', '001)']
        for label in variants:
            self.assertEqual(canonical_id(label), '1', label)
        self.assertEqual(marker_ids('[01]'), ['1'])
        for printed in ['[1]', '1)', '1.', '(1)', '01)']:
            for observed in variants:
                with self.subTest(printed=printed, observed=observed):
                    result = anchor_manuscript({'references_list': [{'ref_id': observed, 'parsed': {
                        'title': 'Underwater optics', 'authors': ['Jane Smith'], 'year': 2020}}]},
                        source_with_entries(f'{printed} Jane Smith. Underwater optics. 2020.'))
                    self.assertEqual(result['references_list'][0]['extraction_status'], 'located')
                    self.assertEqual(result['citations_in_text'][0]['reference_keys'], ['1'])

    def test_meaningful_non_numeric_labels_are_not_reduced_to_a_number(self):
        self.assertNotEqual(canonical_id('Smith (2020)'), canonical_id('Jones (2020)'))
        for value in ['1a', 'S1', '[1,2]', '[1-3]', '1.2', 'Ref. 1', '1) title', '1,']:
            self.assertNotEqual(canonical_id(value), '1', value)

    def test_duplicate_model_labels_remain_ambiguous_after_normalization(self):
        value = {'references_list': [{'ref_id': label, 'parsed': {'title': 'Underwater optics'}}
                                    for label in ['1)', '[1]']]}
        result = anchor_manuscript(value, source_with_entries('1) Underwater optics. 2020.'))
        row = result['references_list'][0]
        self.assertEqual(row['parsed'], {})
        self.assertIn('missing_or_duplicate_model_entry', row['extraction_issues'])
        self.assertEqual(row['extraction_status'], 'needs_review')

    def test_duplicate_printed_labels_do_not_become_two_verified_matches(self):
        value = {'references_list': [{'ref_id': '1)', 'parsed': {'title': 'Underwater optics'}}]}
        result = anchor_manuscript(value, source_with_entries('1) Underwater optics.\n01) Underwater optics.'))
        self.assertEqual(len(result['references_list']), 2)
        for row in result['references_list']:
            self.assertEqual(row['extraction_status'], 'needs_review')
            self.assertIn('duplicate_printed_reference_label', row['extraction_issues'])

    def test_unknown_reference_label_remains_unassigned(self):
        value = {'references_list': [{'ref_id': '99)', 'parsed': {'title': 'Underwater optics'}}]}
        result = anchor_manuscript(value, source_with_entries('1) Underwater optics.'))
        self.assertEqual(result['unassigned_model_references'], value['references_list'])
        self.assertEqual(result['extraction_metadata']['unassigned_model_entries'], 1)

    def test_label_match_never_accepts_a_title_absent_from_the_entry(self):
        value = {'references_list': [{'ref_id': '1)', 'parsed': {'title': 'Invented paper title'}}]}
        result = anchor_manuscript(value, source_with_entries('1) Underwater optics.'))
        self.assertIsNone(result['references_list'][0]['parsed']['title'])
        self.assertIn('title_not_grounded_in_entry', result['references_list'][0]['extraction_issues'])

    def test_restored_metadata_still_rejects_known_wrong_author_candidates(self):
        from src.evidence.identity import resolve_candidates
        _, value = recorded_observations()
        fixed = {r['ref_id']: r for r in anchor_manuscript(value, pdf_source(PDF))['references_list']}
        prior = json.loads((FIXTURES / 'legacy_underwater_result.json').read_text(encoding='utf-8'))
        candidates = {r['ref_id']: r['external_metadata'] for r in prior['scored_references']}
        for label in ['[1]', '[10]']:
            outcome = resolve_candidates([candidates[label]], fixed[label]['parsed'])
            self.assertEqual(outcome['status'], 'unresolved')
            self.assertIn('authors_conflict', outcome['candidates'][0]['conflicts'])
        self.assertEqual(resolve_candidates([candidates['[3]']], fixed['[3]']['parsed'])['status'], 'resolved')

    def test_review_explains_linking_failure_as_system_issue(self):
        from src.evidence import SCHEMA_VERSION
        result = anchor_manuscript({'references_list': []}, source_with_entries('1) Underwater optics.'))
        result['schema_version'] = SCHEMA_VERSION
        result['extraction_metadata']['warnings'] = ['unassigned_model_reference_observations']
        result['scored_references'] = [{'ref_id': '[1]', 'original_data': result['references_list'][0],
            'resolution': {'status': 'extraction_needs_review'}, 'evidence_status': 'identity_unresolved'}]
        review = build_review(result)
        self.assertEqual(review['references'][0]['title'], 'Reference metadata unavailable')
        finding = next(f for f in review['findings'] if f['category'] == 'bibliography_review')
        self.assertTrue(finding['system_issue'])
        self.assertNotIn('missing_or_duplicate_model_entry', finding['explanation'])
        self.assertNotIn('unassigned_model_reference_observations', report_html(result))
        self.assertIn('printed bibliography labels', extraction_message('unassigned_model_reference_observations'))

    def test_fixed_observations_clear_only_stale_unassigned_warning(self):
        _, value = recorded_observations()
        value['unassigned_model_references'] = copy.deepcopy(value['references_list'])
        value['extraction_metadata'] = {'warnings': ['unassigned_model_reference_observations', 'other_warning']}
        result = anchor_manuscript(value, pdf_source(PDF))
        self.assertNotIn('unassigned_model_references', result)
        self.assertEqual(result['extraction_metadata']['warnings'], ['other_warning'])


@unittest.skipUnless(importlib.util.find_spec('pypdfium2'), 'PDF preview dependency is not installed')
class PdfPreviewTests(unittest.TestCase):
    def test_bibliography_page_renders_as_nonempty_bounded_png(self):
        from PIL import Image, ImageStat
        from src.evidence.pdf_preview import render_pdf_page
        png, count = render_pdf_page(PDF.read_bytes(), 6)
        self.assertEqual(count, 6)
        with Image.open(BytesIO(png)) as image:
            self.assertEqual(image.format, 'PNG')
            self.assertLessEqual(max(image.size), 1601)
            self.assertGreater(ImageStat.Stat(image.convert('L')).stddev[0], 10)

    def test_page_bounds_and_preview_size_are_validated(self):
        from src.evidence.pdf_preview import render_pdf_page
        for page in [0, 7]:
            with self.assertRaises(ValueError):
                render_pdf_page(PDF.read_bytes(), page)
        with self.assertRaises(ValueError):
            render_pdf_page(PDF.read_bytes(), 1, max_dimension=100000)

    def test_concurrent_page_previews_are_consistent(self):
        from concurrent.futures import ThreadPoolExecutor
        from src.evidence.pdf_preview import render_pdf_page
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: render_pdf_page(PDF.read_bytes(), 6), range(4)))
        self.assertTrue(all(v == values[0] for v in values))


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit is not installed')
class ReferencePreviewUiTests(unittest.TestCase):
    def test_evidence_renders_a_page_image_and_human_identity_label(self):
        from streamlit.testing.v1 import AppTest
        from src.evidence import SCHEMA_VERSION
        _, value = recorded_observations()
        data = anchor_manuscript(value, pdf_source(PDF))
        data['schema_version'] = SCHEMA_VERSION
        data['scored_references'] = [{'ref_id': r['ref_id'], 'original_data': r,
            'resolution': {'status': 'extraction_needs_review'}, 'evidence_status': 'identity_unresolved'}
            for r in data['references_list']]
        app = AppTest.from_string('''
import streamlit as st
from ui.components.evidence_workspace import render_workspace
render_workspace(st.session_state.artifact, 'Evidence', st.session_state.pdf_path)
''')
        app.session_state.artifact = data
        app.session_state.pdf_path = str(PDF)
        app.run(timeout=15)
        self.assertEqual(len(app.exception), 0, str(app.exception))
        # New Streamlit exposes the public image collection; older versions
        # expose st.image through the legacy "imgs" element type.
        images = getattr(app, 'image', None)
        if images is None:
            images = app.get('imgs')
        self.assertEqual(len(images), 1, 'Expected one manuscript page preview. '
            f'UI notices: {[item.value for item in app.info]}; '
            f'UI errors: {[item.value for item in app.error]}')
        self.assertEqual(len(images[0].proto.imgs), 1)
        self.assertEqual(images[0].proto.imgs[0].caption, 'Manuscript page 6 of 6')
        self.assertTrue(images[0].proto.imgs[0].url)
        self.assertEqual(app.selectbox[1].value, 6)
        self.assertFalse(app.get('iframe'))
        self.assertTrue(any('Bibliography needs review' in item.value for item in app.markdown))

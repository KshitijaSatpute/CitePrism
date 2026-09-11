"""Behavior checks for the interim reviewer workspace (no browser/network required)."""
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest
from tests.evidence_fixtures import scored


class EvidenceUiTests(unittest.TestCase):
    def test_review_evidence_and_diagnostics_render_same_reference_set(self):
        with tempfile.TemporaryDirectory() as directory:
            value = scored(Path(directory))
            for mode in ('Review', 'Evidence', 'Diagnostics'):
                with self.subTest(mode=mode):
                    app = AppTest.from_string('''
import streamlit as st
from ui.components.evidence_workspace import render_workspace
render_workspace(st.session_state.artifact, st.session_state.mode)
''')
                    app.session_state.artifact = value
                    app.session_state.mode = mode
                    app.run(timeout=15)
                    self.assertEqual(len(app.exception), 0, str(app.exception))
                    self.assertEqual(app.metric[0].value, '10')
                    self.assertEqual(app.metric[1].value, '6')
                    if mode == 'Review':
                        self.assertEqual(len(app.dataframe[0].value), 10)
                        self.assertEqual(len(app.get('download_button')), 2)
                    if mode == 'Evidence':
                        self.assertEqual(len(app.selectbox[0].options), 10)

    def test_review_action_selects_evidence_and_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            app = AppTest.from_string('''
import streamlit as st
from ui.components.evidence_workspace import render_workspace
render_workspace(st.session_state.artifact, 'Review')
''')
            app.session_state.artifact = scored(Path(directory))
            app.run(timeout=15)
            app.button[0].click().run(timeout=15)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.session_state.active_tab, 'Evidence')
            self.assertTrue(app.session_state.selected_evidence_reference)

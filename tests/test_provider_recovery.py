"""Regression of the reported 200(metadata), 503(bibliography) failure sequence."""

import json
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.evidence.providers import CallRecorder, JsonProvider, ProviderFailure, classify_error
from src.evidence.extraction import anchor_manuscript, pdf_source
from src.extractors.gemini_extractor import GeminiExtractor, GoogleInterface

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"
PDF = FIXTURES / "underwater_manuscript.pdf"


def installed(module):
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


class ServiceError(Exception):
    def __init__(self, code=503, retry_after=None):
        super().__init__("Private provider message: secret-do-not-export")
        self.code = code
        self.response = SimpleNamespace(status_code=code, headers={"Retry-After": retry_after})


class ProviderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        original = json.loads((FIXTURES / "legacy_underwater_result.json").read_text(encoding="utf-8"))
        self.value = anchor_manuscript({"metadata": original["manuscript_metadata"],
            "references_list": [r["original_data"] for r in original["scored_references"]]}, pdf_source(PDF))
        self.metadata = json.dumps({"metadata": self.value["metadata"]})
        self.references = json.dumps({"references_list": self.value["references_list"]})

    def interface(self, responses, model="test-model"):
        interface = object.__new__(GoogleInterface)
        interface.model_name = model
        interface.max_tokens = 32000
        recorder = CallRecorder(self.root / "calls", retry_delay=0)
        interface.provider = SimpleNamespace(provider="google", recorder=recorder)
        interface.transport = Mock(side_effect=responses)

        def generate(prompt):
            return recorder.call(provider="google", model=model, purpose="extraction",
                                 prompt_version="test", request={"prompt": prompt},
                                 invoke=interface.transport, decode=lambda r: (r, None))
        interface._call_api = generate
        return interface

    def extractor(self, interface):
        config = SimpleNamespace(DATA_DIR=self.root, GOOGLE_MODEL=interface.model_name)
        extractor = GeminiExtractor(config)
        extractor._get_llm_interface = Mock(return_value=interface)
        return extractor

    def test_503_is_specific_bounded_and_records_backoff_without_private_error(self):
        recorder = CallRecorder(self.root)
        invoke = Mock(side_effect=ServiceError())
        progress = Mock()
        recorder.progress_callback = progress
        with patch("src.evidence.providers.time.sleep") as sleep, patch(
                "src.evidence.providers.random.uniform", return_value=0):
            with self.assertRaises(ProviderFailure) as raised:
                recorder.call(provider="google", model="test", purpose="test", prompt_version="v1",
                              request={"prompt": "test"}, invoke=invoke, decode=lambda x: x)
        self.assertEqual(classify_error(ServiceError()), "service_unavailable")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.attempts, 3)
        self.assertTrue(raised.exception.retryable)
        self.assertIn("temporarily unavailable", str(raised.exception))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])
        self.assertEqual(progress.call_count, 2)
        self.assertEqual(invoke.call_count, 3)
        record = json.loads(next(self.root.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual([a["http_status"] for a in record["attempts"]], [503, 503, 503])
        self.assertNotIn("secret-do-not-export", json.dumps(record))

    def test_provider_retry_after_is_honored_and_long_wait_stops(self):
        for delay, calls, waits in [(20, 2, [20]), (120, 1, [])]:
            with self.subTest(delay=delay):
                recorder = CallRecorder(retry_delay=0)
                invoke = Mock(side_effect=[ServiceError(429, str(delay)), "{}"])
                with patch("src.evidence.providers.time.sleep") as sleep:
                    try:
                        recorder.call(provider="google", model="test", purpose="test", prompt_version="v1",
                                      request={}, invoke=invoke, decode=lambda r: (r, None))
                    except ProviderFailure as error:
                        self.assertEqual(delay, 120)
                        self.assertEqual(error.code, "rate_limit")
                self.assertEqual(invoke.call_count, calls)
                self.assertEqual([c.args[0] for c in sleep.call_args_list], waits)

    def test_failed_bibliography_resumes_after_restart_with_original_call_evidence(self):
        first = self.interface([self.metadata, ServiceError(), ServiceError(), ServiceError()])
        with self.assertRaises(ProviderFailure):
            self.extractor(first).extract(PDF)
        checkpoint = next(self.root.rglob("*.checkpoint.json"))
        partial = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(list(partial["completed"]), ["manuscript metadata"])
        self.assertEqual(len(partial["provider_runs"]), 2)
        self.assertEqual(partial["provider_runs"][-1]["status"], "failed")
        second = self.interface([self.references])
        result = self.extractor(second).extract(PDF)
        self.assertEqual(second.transport.call_count, 1)
        self.assertEqual(result["extraction_metadata"]["checkpoint_steps_reused"], 1)
        self.assertEqual(len(result["references_list"]), 10)
        self.assertEqual(len(result["citations_in_text"]), 6)
        self.assertEqual([r["status"] for r in result["provider_runs"]], ["succeeded", "failed", "succeeded"])
        self.assertEqual(result["provider_runs"][0], partial["provider_runs"][0])
        self.assertFalse(checkpoint.exists())

    def test_changed_model_does_not_reuse_partial_metadata(self):
        first = self.interface([self.metadata, ServiceError(), ServiceError(), ServiceError()])
        with self.assertRaises(ProviderFailure):
            self.extractor(first).extract(PDF)
        second = self.interface([self.metadata, self.references], model="different-model")
        result = self.extractor(second).extract(PDF)
        self.assertEqual(second.transport.call_count, 2)
        self.assertEqual(result["extraction_metadata"]["checkpoint_steps_reused"], 0)
        self.assertEqual(len(result["provider_runs"]), 2)

    def test_authentication_failure_stops_without_retry_or_completed_extraction(self):
        first = self.interface([ServiceError(401)])
        with self.assertRaises(ProviderFailure) as raised:
            self.extractor(first).extract(PDF)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.code, "authentication")
        self.assertEqual(first.transport.call_count, 1)
        checkpoint = next(self.root.rglob("*.checkpoint.json"))
        self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8"))["completed"], {})

    def test_pdf_identity_change_does_not_reuse_same_text_checkpoint(self):
        text = "A body cites [1].\n\nReferences\n[1] Smith. A specific title. 2020."
        debug_path = self.root / "response.json"
        first = self.interface([self.metadata, ServiceError(), ServiceError(), ServiceError()])
        with self.assertRaises(ProviderFailure):
            first.parse_manuscript(text, debug_path=debug_path, document_sha256="a" * 64)
        second = self.interface([self.metadata, self.references])
        result = second.parse_manuscript(text, debug_path=debug_path, document_sha256="b" * 64)
        self.assertEqual(second.transport.call_count, 2)
        self.assertEqual(result["extraction_metadata"]["checkpoint_steps_reused"], 0)

    def test_partial_bibliography_keeps_completed_chunks_when_later_chunk_fails(self):
        text = "An underwater manuscript.\n\nReferences\n" + "\n".join(
            f"[{i}] Smith. " + "Detailed reference title with technical measurements. " * 2
            for i in range(1, 4))
        refs = [{"ref_id": f"[{i}]", "parsed": {"title": f"Title {i}"}} for i in range(1, 4)]
        responses = [json.dumps({"references_list": [r]}) for r in refs]
        first = self.interface([self.metadata, responses[0], ServiceError(), ServiceError(), ServiceError()])
        first.CHUNK_SIZE = 160
        debug_path = self.root / "response.json"
        with self.assertRaises(ProviderFailure):
            first.parse_manuscript(text, debug_path=debug_path)
        second = self.interface(responses[1:])
        second.CHUNK_SIZE = 160
        result = second.parse_manuscript(text, debug_path=debug_path)
        self.assertEqual(second.transport.call_count, 2)
        self.assertEqual(result["extraction_metadata"]["checkpoint_steps_reused"], 2)
        self.assertEqual([r["ref_id"] for r in result["references_list"]], ["[1]", "[2]", "[3]"])

    def test_malformed_metadata_is_not_checkpointed_as_a_successful_step(self):
        first = self.interface(['{"metadata": []}'])
        with self.assertRaises(ValueError):
            self.extractor(first).extract(PDF)
        checkpoint = next(self.root.rglob("*.checkpoint.json"))
        self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8"))["completed"], {})
        second = self.interface([self.metadata, self.references])
        self.assertEqual(self.extractor(second).extract(PDF)["extraction_metadata"]["checkpoint_steps_reused"], 0)
        self.assertEqual(second.transport.call_count, 2)

    def test_corrupt_checkpoint_is_not_used(self):
        first = self.interface([self.metadata, ServiceError(), ServiceError(), ServiceError()])
        with self.assertRaises(ProviderFailure):
            self.extractor(first).extract(PDF)
        checkpoint = next(self.root.rglob("*.checkpoint.json"))
        value = json.loads(checkpoint.read_text(encoding="utf-8"))
        value["completed"]["manuscript metadata"]["title"] = "Changed title"
        checkpoint.write_text(json.dumps(value), encoding="utf-8")
        second = self.interface([self.metadata, self.references])
        result = self.extractor(second).extract(PDF)
        self.assertEqual(result["metadata"]["title"], self.value["metadata"]["title"])
        self.assertEqual(second.transport.call_count, 2)

    @unittest.skipUnless(installed("google.genai"), "Google SDK is not installed in this test environment")
    def test_google_generation_disables_unused_automatic_function_calling(self):
        from google.genai import types
        provider = JsonProvider("google", "test-not-a-real-key", "test-model", 1024)
        self.addCleanup(provider.client.close)
        response = SimpleNamespace(text='{"ok": true}', usage_metadata=None)
        with patch.object(provider.client.models, "generate_content", return_value=response) as call:
            provider.generate("Return JSON")
        config = types.GenerateContentConfig(**call.call_args.kwargs["config"])
        self.assertTrue(config.automatic_function_calling.disable)
        self.assertFalse(provider.recorder.records[0]["request"]["automatic_function_calling"])

    @unittest.skipUnless(installed("streamlit"), "Streamlit is not installed in this test environment")
    def test_ui_distinguishes_temporary_outage_from_authentication_error(self):
        from streamlit.testing.v1 import AppTest
        for code in ("service_unavailable", "authentication"):
            error = ProviderFailure(code, "google", status_code=503 if code == "service_unavailable" else 401)
            app = AppTest.from_string('''
import streamlit as st
from ui.tabs.tab_1_upload import render_processing_failure
render_processing_failure(st.session_state.result)
''')
            app.session_state.result = {"errors": [str(error)], "provider_failure": {
                "code": code, "retryable": error.retryable}}
            app.run(timeout=15)
            self.assertEqual(len(app.exception), 0, str(app.exception))
            if error.retryable:
                self.assertEqual(len(app.error), 0)
                self.assertIn("Run citation audit", app.info[0].value)
                self.assertIn("incomplete", app.warning[0].value)
            else:
                self.assertEqual(len(app.warning), 0)
                self.assertIn("API key", app.error[1].value)

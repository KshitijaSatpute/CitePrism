"""Where manuscript text goes, and on what terms (5.4).

Google's Gemini API terms for unpaid services say submitted content is used to
improve its products, may be read by human reviewers, and that sensitive or
confidential information must not be submitted. A manuscript under peer review
is confidential, so CitePrism names the services that receive manuscript text
and can enforce a local-only configuration.
"""

import unittest
from types import SimpleNamespace

from src.workspace.data_policy import data_handling, is_local_url, policy_errors


def config(**changes):
    values = dict(LLM_PROVIDER="google", LLM_FALLBACK_MODELS=None, ASSESSMENT_PROVIDER="huggingface",
                  SIMILARITY_BACKEND="auto", OPENAI_COMPATIBLE_BASE_URL=None, MODEL_DATA_TERMS="unverified",
                  OFFLINE_MODE=False, RESOLUTION_PROVIDERS=("openalex", "crossref"))
    values.update(changes)
    return SimpleNamespace(**values)


class DataPolicyTests(unittest.TestCase):
    def test_the_default_names_every_service_that_receives_manuscript_text(self):
        handling = data_handling(config())
        self.assertEqual(handling["terms"], "unverified")
        self.assertEqual(handling["model_recipients"], ["Google Gemini", "Hugging Face"])
        self.assertEqual(handling["reference_lookups"], ["OpenAlex", "Crossref"])

    def test_fallback_models_and_remote_compatible_services_are_named(self):
        handling = data_handling(config(LLM_FALLBACK_MODELS="openai_compatible:llama-3.3-70b",
                                        OPENAI_COMPATIBLE_BASE_URL="https://api.groq.com/openai/v1",
                                        ASSESSMENT_PROVIDER="openai_compatible"))
        self.assertEqual(handling["model_recipients"], ["Google Gemini", "Groq"])

    def test_local_only_is_enforced(self):
        errors = policy_errors(config(MODEL_DATA_TERMS="local_only"))
        self.assertTrue(any("reading the PDF uses Google Gemini" in error for error in errors))
        self.assertTrue(any("checking citations uses Hugging Face" in error for error in errors))
        local = config(MODEL_DATA_TERMS="local_only", LLM_PROVIDER="openai_compatible",
                       ASSESSMENT_PROVIDER="openai_compatible", LLM_FALLBACK_MODELS="none",
                       OPENAI_COMPATIBLE_BASE_URL="http://localhost:11434/v1")
        self.assertEqual(policy_errors(local), [])
        self.assertEqual(data_handling(local)["model_recipients"], ["a local model"])
        remote = config(MODEL_DATA_TERMS="local_only", LLM_PROVIDER="openai_compatible",
                        ASSESSMENT_PROVIDER="openai_compatible", LLM_FALLBACK_MODELS="none",
                        OPENAI_COMPATIBLE_BASE_URL="https://api.groq.com/openai/v1")
        self.assertTrue(policy_errors(remote))

    def test_unknown_terms_are_rejected(self):
        self.assertEqual(policy_errors(config(MODEL_DATA_TERMS="trust_me")),
                         ["CITEPRISM_MODEL_DATA_TERMS must be unverified, no_training or local_only"])

    def test_offline_mode_counts_as_local(self):
        handling = data_handling(config(OFFLINE_MODE=True, LLM_PROVIDER="openai_compatible",
                                        ASSESSMENT_PROVIDER="openai_compatible",
                                        OPENAI_COMPATIBLE_BASE_URL="http://ollama:11434/v1"))
        self.assertEqual((handling["terms"], handling["reference_lookups"]), ("local_only", []))

    def test_local_hosts(self):
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:8000", "http://10.0.0.5/v1", "http://ollama:11434",
                    "http://gpu-box.local/v1", "http://[::1]:8080"):
            self.assertTrue(is_local_url(url), url)
        for url in ("https://api.groq.com/openai/v1", "https://8.8.8.8/v1", "", None):
            self.assertFalse(is_local_url(url), url)

    def test_the_real_configuration_validates_the_terms(self):
        from config.settings import Config
        original = Config.MODEL_DATA_TERMS
        try:
            Config.MODEL_DATA_TERMS = "nonsense"
            self.assertIn("CITEPRISM_MODEL_DATA_TERMS must be unverified, no_training or local_only",
                          Config.phase4_errors())
        finally:
            Config.MODEL_DATA_TERMS = original


if __name__ == "__main__":
    unittest.main()

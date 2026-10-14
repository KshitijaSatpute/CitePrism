"""Model fallback: an overloaded primary model must not stop an audit."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.evidence.providers import (
    CallRecorder,
    FallbackJsonProvider,
    JsonProvider,
    ProviderFailure,
    build_json_provider,
    parse_fallback_chain,
    supports_thinking_budget,
)


class Overloaded(Exception):
    def __init__(self, code=503):
        super().__init__("model is overloaded")
        self.code = code
        self.response = SimpleNamespace(status_code=code, headers={})


def fake_target(recorder, provider, model, outcomes):
    """A JsonProvider whose client returns or raises the given outcomes in order."""
    target = object.__new__(JsonProvider)
    target.provider, target.model, target.max_tokens = provider, model, 100
    target.recorder, target.thinking_budget = recorder, None
    calls = iter(outcomes)

    def create(**_):
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))],
                               usage=None)

    target.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return target


class FallbackChainParsingTests(unittest.TestCase):
    def test_gemini_gets_lighter_models_by_default(self):
        self.assertEqual(parse_fallback_chain(None, "google"),
                         [("google", "gemini-3.1-flash-lite"), ("google", "gemini-3.5-flash")])

    def test_other_providers_have_no_implicit_fallback(self):
        self.assertEqual(parse_fallback_chain("", "openai"), [])

    def test_bare_names_use_the_primary_provider_and_prefixes_switch_provider(self):
        self.assertEqual(parse_fallback_chain("gemini-2.0-flash, openai_compatible:llama-3.3-70b", "google"),
                         [("google", "gemini-2.0-flash"), ("openai_compatible", "llama-3.3-70b")])

    def test_none_disables_fallback(self):
        self.assertEqual(parse_fallback_chain("none", "google"), [])

    def test_unknown_provider_is_a_configuration_error(self):
        with self.assertRaises(ValueError):
            parse_fallback_chain("anthropic:claude", "google")

    def test_thinking_budget_only_for_gemini_25_flash(self):
        self.assertTrue(supports_thinking_budget("gemini-2.5-flash"))
        self.assertTrue(supports_thinking_budget("models/gemini-2.5-flash-lite"))
        self.assertFalse(supports_thinking_budget("gemini-2.5-pro"))
        self.assertFalse(supports_thinking_budget("gemini-3.6-flash"))
        self.assertFalse(supports_thinking_budget("llama3.1"))


class FallbackProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.recorder = CallRecorder(Path(self.temp.name), max_attempts=2, retry_delay=0)

    def test_overloaded_primary_falls_back_and_records_both(self):
        primary = fake_target(self.recorder, "openai_compatible", "big", [Overloaded(), Overloaded()])
        backup = fake_target(self.recorder, "openai_compatible", "small", ['{"ok": true}'])
        chain = FallbackJsonProvider([primary, backup])
        self.assertEqual(chain.generate("prompt"), '{"ok": true}')
        self.assertEqual(chain.model, "small")
        statuses = [(run["model"], run["status"]) for run in self.recorder.records]
        self.assertEqual(statuses, [("big", "failed"), ("small", "succeeded")])

    def test_authentication_errors_are_not_hidden_by_fallback(self):
        primary = fake_target(self.recorder, "openai_compatible", "big", [Overloaded(401)])
        backup = fake_target(self.recorder, "openai_compatible", "small", ['{}'])
        with self.assertRaises(ProviderFailure) as raised:
            FallbackJsonProvider([primary, backup]).generate("prompt")
        self.assertEqual(raised.exception.code, "authentication")
        self.assertEqual(len(self.recorder.records), 1)

    def test_when_every_model_fails_the_message_names_them_and_the_remedy(self):
        targets = [fake_target(self.recorder, "openai_compatible", name, [Overloaded(), Overloaded()])
                   for name in ("a", "b")]
        with self.assertRaises(ProviderFailure) as raised:
            FallbackJsonProvider(targets).generate("prompt")
        message = str(raised.exception)
        self.assertEqual(raised.exception.code, "service_unavailable")
        self.assertIn("openai_compatible:a", message)
        self.assertIn("LLM_FALLBACK_MODELS", message)

    def test_success_on_primary_does_not_touch_fallbacks(self):
        primary = fake_target(self.recorder, "openai_compatible", "big", ['{"x": 1}'])
        backup = fake_target(self.recorder, "openai_compatible", "small", [])
        chain = FallbackJsonProvider([primary, backup])
        chain.generate("prompt")
        self.assertEqual(chain.model, "big")
        self.assertEqual(len(self.recorder.records), 1)


class GeminiRequestTests(unittest.TestCase):
    def gemini(self, model, budget):
        captured = {}
        target = object.__new__(JsonProvider)
        target.provider, target.model, target.max_tokens = "google", model, 100
        target.recorder, target.thinking_budget = CallRecorder(None, retry_delay=0), budget

        def generate_content(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text="{}", usage_metadata=None)

        target.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        target.generate("prompt")
        return captured["config"], target.recorder.records[-1]["request"]

    def test_flash_extraction_runs_without_thinking(self):
        config, request = self.gemini("gemini-2.5-flash", 0)
        self.assertEqual(config["thinking_config"], {"thinking_budget": 0})
        self.assertEqual(request["thinking_budget"], 0)

    def test_pro_and_default_budget_leave_the_model_setting_alone(self):
        self.assertNotIn("thinking_config", self.gemini("gemini-2.5-pro", 0)[0])
        self.assertNotIn("thinking_config", self.gemini("gemini-2.5-flash", None)[0])


class OutputCapTests(unittest.TestCase):
    def test_openai_compatible_requests_are_capped_for_free_tier_limits(self):
        from src.evidence.providers import output_cap
        config = SimpleNamespace(OPENAI_COMPATIBLE_MAX_TOKENS=6000)
        self.assertEqual(output_cap(config, "openai_compatible", 32000), 6000)
        self.assertEqual(output_cap(config, "openai_compatible", 1200), 1200)
        self.assertEqual(output_cap(config, "google", 32000), 32000)


class BuildProviderTests(unittest.TestCase):
    def config(self, **values):
        base = dict(GOOGLE_API_KEY=None, OPENAI_API_KEY=None, OPENAI_COMPATIBLE_API_KEY=None,
                    OPENAI_COMPATIBLE_BASE_URL=None, LLM_FALLBACK_MODELS=None,
                    GOOGLE_THINKING_BUDGET="0", OFFLINE_MODE=False)
        base.update(values)
        return SimpleNamespace(**base)

    def test_fallbacks_without_credentials_are_skipped_not_fatal(self):
        try:
            import openai  # noqa: F401
        except ImportError:
            self.skipTest("openai SDK not installed")
        provider = build_json_provider(
            self.config(OPENAI_COMPATIBLE_BASE_URL="http://localhost:11434/v1",
                        LLM_FALLBACK_MODELS="openai:gpt-4o-mini"),
            "openai_compatible", "llama3.1", 100)
        self.assertIsInstance(provider, JsonProvider)

    def test_offline_mode_never_falls_back_to_a_hosted_model(self):
        try:
            import openai  # noqa: F401
        except ImportError:
            self.skipTest("openai SDK not installed")
        provider = build_json_provider(
            self.config(OPENAI_COMPATIBLE_BASE_URL="http://localhost:11434/v1", OFFLINE_MODE=True,
                        OPENAI_API_KEY="placeholder", LLM_FALLBACK_MODELS="openai:gpt-4o-mini"),
            "openai_compatible", "llama3.1", 100)
        self.assertIsInstance(provider, JsonProvider)


if __name__ == "__main__":
    unittest.main()

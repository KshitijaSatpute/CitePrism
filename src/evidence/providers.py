"""Bounded provider calls with exact local request/response artifacts and safe errors."""

import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from src.evidence.text import digest_json
from src.utils.safe_io import atomic_write_json

logger = logging.getLogger(__name__)
TRANSIENT_ERRORS = {"service_unavailable", "rate_limit", "timeout", "provider_error"}
# Failures that another model or provider may not share. Authentication and
# request errors are configuration problems and are reported, not hidden.
FALLBACK_ERRORS = TRANSIENT_ERRORS | {"model_unavailable", "empty_response"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ProviderFailure(RuntimeError):
    def __init__(self, code, provider, *, status_code=None, attempts=None, tried=None):
        self.code = code
        self.tried = list(tried or [])
        self.provider = provider
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = code in TRANSIENT_ERRORS
        actions = {"authentication": "Check the configured API key and access permissions.",
                   "service_unavailable": "The service is temporarily unavailable (HTTP 503). Wait a few minutes, then retry the audit.",
                   "model_unavailable": "The configured model is not available (HTTP 404); it may have been retired. Set a current model, e.g. GOOGLE_MODEL=gemini-3.6-flash.",
                   "rate_limit": "Provider quota or rate limit reached; retry later.",
                   "timeout": "Provider timed out; retry this stage.",
                   "invalid_request": "The provider rejected the request configuration.",
                   "empty_response": "The provider returned no usable text.",
                   "provider_error": "The provider could not complete this request."}
        message = f"{provider}: {actions.get(code, actions['provider_error'])}"
        if len(self.tried) > 1:
            message = (f"Every configured model failed ({', '.join(self.tried)}); the last error was "
                       f"{message} Add another provider to LLM_FALLBACK_MODELS in .env "
                       f"(for example a free Groq key or a local Ollama model) to keep audits running.")
        super().__init__(message)


def http_status(error):
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    response = getattr(error, "response", None)
    if response is not None:
        status = status or getattr(response, "status_code", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def classify_error(error):
    status = http_status(error)
    message = str(error).lower()  # inspected locally, never copied into artifacts/logs
    if status in (401, 403) or "api_key_invalid" in message or "api key not valid" in message:
        return "authentication"
    if status == 404:
        return "model_unavailable"
    if status == 429:
        return "rate_limit"
    if status == 503:
        return "service_unavailable"
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower():
        return "timeout"
    if status in (400, 422):
        return "invalid_request"
    return "provider_error"


def retry_after_seconds(error):
    """Read a provider delay without retaining headers that may contain secrets."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or getattr(error, "headers", None) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            stamp = parsedate_to_datetime(str(value))
            delay = (stamp - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    # Reject NaN/negative values; infinity exceeds the retry budget and stops.
    return delay if delay >= 0 else None


class CallRecorder:
    def __init__(self, directory=None, max_attempts=3, retry_delay=5.0):
        self.directory = Path(directory) if directory else None
        self.max_attempts = min(3, max(1, max_attempts))
        self.retry_delay = min(10.0, max(0.0, retry_delay))
        self.records = []
        self.progress_callback = None

    def call(self, *, provider, model, purpose, prompt_version, request, invoke, decode):
        request_hash = digest_json(request)
        started = utc_now()
        identifier = str(uuid.uuid4())
        record = {"id": identifier, "provider": provider, "model": model, "purpose": purpose,
                  "prompt_version": prompt_version, "input_sha256": request_hash,
                  "started_at": started, "attempts": [], "provider_execution_observed": True,
                  "request": request}
        # The request contains prompts/configuration only, never client credentials.
        path = self.directory / f"{identifier}.json" if self.directory else None
        if path:
            atomic_write_json(path, {**record, "status": "started"})
        beginning = time.perf_counter()
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = invoke()
                payload, usage = decode(response)
                if not isinstance(payload, str) or not payload.strip():
                    raise ProviderFailure("empty_response", provider)
                record.update({"status": "succeeded", "response": payload,
                               "output_sha256": digest_json(payload), "usage": usage})
                record["attempts"].append({"attempt": attempt, "status": "succeeded"})
                break
            except Exception as error:
                code = error.code if isinstance(error, ProviderFailure) else classify_error(error)
                status = http_status(error)
                detail = {"attempt": attempt, "status": "failed", "error_code": code,
                          "http_status": status}
                record["attempts"].append(detail)
                retry = code in TRANSIENT_ERRORS and attempt < self.max_attempts
                if retry:
                    base_delay = self.retry_delay * (2 ** (attempt - 1))
                    delay = min(30.0, base_delay + random.uniform(0, min(1.0, base_delay / 4)))
                    requested_delay = retry_after_seconds(error)
                    if requested_delay is not None and requested_delay > 30:
                        # Do not retry earlier than the provider permits or block indefinitely.
                        detail["retry_stopped"] = "provider_delay_exceeds_30_second_budget"
                    else:
                        delay = max(delay, requested_delay or 0)
                        detail["retry_delay_seconds"] = round(delay, 3)
                        if path:
                            atomic_write_json(path, {**record, "status": "retrying"})
                        message = (f"{provider}: {code.replace('_', ' ')}. "
                                   f"Retry {attempt + 1}/{self.max_attempts} in {delay:.1f}s.")
                        logger.warning(message)
                        if self.progress_callback:
                            try:
                                self.progress_callback(message)
                            except Exception:
                                logger.debug("Progress display unavailable; provider retry will continue.")
                        time.sleep(delay)
                        continue
                record.update({"status": "failed", "error_code": code, "http_status": status})
                self._finish(record, beginning, path)
                raise ProviderFailure(code, provider, status_code=status, attempts=attempt) from None
        self._finish(record, beginning, path)
        return payload

    def _finish(self, record, beginning, path):
        record["completed_at"] = utc_now()
        record["elapsed_seconds"] = round(time.perf_counter() - beginning, 4)
        if path:
            atomic_write_json(path, record)
        self.records.append(record)


def usage_dict(value):
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return None


class JsonProvider:
    """Maintained Google SDK and existing OpenAI SDK behind the same extraction port."""

    def __init__(self, provider, api_key, model, max_tokens, directory=None, base_url=None,
                 recorder=None, thinking_budget=None):
        self.provider, self.model, self.max_tokens = provider, model, max_tokens
        self.recorder = recorder or CallRecorder(directory)
        self.thinking_budget = thinking_budget
        if provider == "openai_compatible":
            # A local or self-hosted server speaking the OpenAI chat API: Ollama,
            # vLLM, LM Studio, llama.cpp server. This is the offline deployment
            # path; no data leaves the machine or network that hosts it.
            if not base_url:
                raise ProviderFailure("invalid_request", provider)
            if not model:
                raise ProviderFailure("model_unavailable", provider)
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key or "not-required", base_url=base_url,
                                 timeout=300, max_retries=0)
            return
        if not api_key:
            raise ProviderFailure("authentication", provider)
        if provider == "google":
            from google import genai
            # The app owns the bounded retry policy; SDK timeout is milliseconds.
            self.client = genai.Client(api_key=api_key, http_options={"timeout": 120000,
                                      "retry_options": {"attempts": 1}})
        elif provider == "openai":
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key, timeout=120, max_retries=0)
        else:
            raise ValueError("Unsupported extraction provider")

    def generate(self, prompt, purpose="extraction", prompt_version="extraction-v3"):
        request = {"prompt": prompt, "temperature": 0.1, "max_output_tokens": self.max_tokens,
                   "response_format": "json", "timeout_seconds": 120}
        if self.provider == "google":
            request["automatic_function_calling"] = False
            generation = {"temperature": 0.1, "max_output_tokens": self.max_tokens,
                          "response_mime_type": "application/json",
                          "automatic_function_calling": {"disable": True}}
            if self.thinking_budget is not None and supports_thinking_budget(self.model):
                # Structured extraction needs no reasoning trace. Thinking spends the
                # output budget and lengthens calls, which is when Flash answers 503.
                generation["thinking_config"] = {"thinking_budget": int(self.thinking_budget)}
                request["thinking_budget"] = int(self.thinking_budget)
            invoke = lambda: self.client.models.generate_content(
                model=self.model, contents=prompt, config=generation)
            decode = lambda response: (response.text, usage_dict(response.usage_metadata))
        else:
            invoke = lambda: self.client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, max_tokens=self.max_tokens, temperature=0.1)
            if self.provider == "openai_compatible":
                request["timeout_seconds"] = 300
                request["endpoint"] = "openai_compatible"
            decode = lambda response: (response.choices[0].message.content, usage_dict(response.usage))
        return self.recorder.call(provider=self.provider, model=self.model, purpose=purpose,
                                  prompt_version=prompt_version, request=request, invoke=invoke, decode=decode)


def strict_json(text):
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    # Never repair a truncated response into an apparently complete result.
    def reject_constant(_):
        raise ValueError("Non-finite numbers are not valid evidence JSON")
    return json.loads(value, parse_constant=reject_constant)


def supports_thinking_budget(model):
    """Gemini 2.5 Flash and Flash-Lite accept a zero thinking budget; Pro does not.

    Gemini 3 models use a different control (thinking level). It is left at the
    model default rather than sending a parameter the model might reject.
    """
    name = str(model or "").lower().split("/")[-1]
    return name.startswith("gemini-2.5-flash")


DEFAULT_FALLBACKS = {
    # Current stable models with a free tier, each with its own quota. Gemini 2.0
    # Flash was shut down on 2026-06-01 and 2.5 Flash is discontinued on
    # 2026-10-16 (ai.google.dev/gemini-api/docs/deprecations); a retired model
    # answers 404 "model_unavailable", which this chain treats as "try the next".
    "google": ("gemini-3.1-flash-lite", "gemini-3.5-flash"),
}


def parse_fallback_chain(spec, primary_provider):
    """`"gemini-3.1-flash-lite, openai_compatible:llama3.1"` -> [(provider, model), ...].

    A bare model name means "same provider as the primary". `none` disables
    fallback. An empty or missing value uses `DEFAULT_FALLBACKS`.
    """
    if spec is None or not str(spec).strip():
        return [(primary_provider, model) for model in DEFAULT_FALLBACKS.get(primary_provider, ())]
    text = str(spec).strip()
    if text.lower() in {"none", "off", "false", "0"}:
        return []
    chain = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        provider, _, model = item.partition(":")
        if not model:
            provider, model = primary_provider, provider
        provider = provider.strip().lower()
        if provider not in {"google", "openai", "openai_compatible"}:
            raise ValueError(f"LLM_FALLBACK_MODELS: unknown provider '{provider}' in '{item}'")
        chain.append((provider, model.strip()))
    return chain


class FallbackJsonProvider:
    """Try the primary model, then each fallback, on failures another model may not share.

    Every attempt on every model is written by one shared `CallRecorder`, so
    the audit's provider record shows exactly which model produced each answer.
    """

    def __init__(self, targets):
        if not targets:
            raise ValueError("At least one model is required")
        self.targets = list(targets)
        self.recorder = self.targets[0].recorder
        self.active = self.targets[0]

    @property
    def provider(self):
        return self.active.provider

    @property
    def model(self):
        return self.active.model

    @property
    def models(self):
        return [f"{target.provider}:{target.model}" for target in self.targets]

    def generate(self, prompt, purpose="extraction", prompt_version="extraction-v3"):
        tried, last = [], None
        for target in self.targets:
            tried.append(f"{target.provider}:{target.model}")
            try:
                result = target.generate(prompt, purpose=purpose, prompt_version=prompt_version)
            except ProviderFailure as failure:
                if failure.code not in FALLBACK_ERRORS:
                    raise
                last = failure
                if target is not self.targets[-1]:
                    logger.warning("%s:%s failed (%s); falling back to the next configured model.",
                                   target.provider, target.model, failure.code)
                continue
            if target is not self.active:
                logger.info("Using fallback model %s:%s.", target.provider, target.model)
            self.active = target
            return result
        raise ProviderFailure(last.code, last.provider, status_code=last.status_code,
                              attempts=last.attempts, tried=tried)


def provider_credentials(config, provider):
    """(api_key, base_url) for a provider from configuration."""
    if provider == "google":
        return getattr(config, "GOOGLE_API_KEY", None), None
    if provider == "openai":
        return getattr(config, "OPENAI_API_KEY", None), None
    if provider == "openai_compatible":
        return (getattr(config, "OPENAI_COMPATIBLE_API_KEY", None),
                getattr(config, "OPENAI_COMPATIBLE_BASE_URL", None))
    raise ValueError(f"Unsupported provider: {provider}")


def output_cap(config, provider, max_tokens):
    """Hosted OpenAI-compatible free tiers (Groq, OpenRouter) count the *requested*
    output against a per-minute token limit, so a 32k request is refused outright.
    OpenAI-compatible targets therefore get their own, smaller cap."""
    if provider != "openai_compatible":
        return max_tokens
    cap = int(getattr(config, "OPENAI_COMPATIBLE_MAX_TOKENS", 8192) or 8192)
    return max(256, min(int(max_tokens), cap))


def build_json_provider(config, provider, model, max_tokens, directory=None):
    """The configured model plus its fallbacks, sharing one call recorder.

    A fallback whose credentials are missing is skipped with a warning rather
    than failing the audit before the primary model has even been tried.
    """
    recorder = CallRecorder(directory)
    thinking = str(getattr(config, "GOOGLE_THINKING_BUDGET", "0") or "default").strip().lower()
    thinking = None if thinking == "default" else int(thinking)
    api_key, base_url = provider_credentials(config, provider)
    targets = [JsonProvider(provider, api_key, model, output_cap(config, provider, max_tokens), directory,
                            base_url=base_url, recorder=recorder, thinking_budget=thinking)]
    seen = {(provider, model)}
    offline = bool(getattr(config, "OFFLINE_MODE", False))
    chain = parse_fallback_chain(getattr(config, "LLM_FALLBACK_MODELS", None), provider)
    for fallback_provider, fallback_model in chain:
        if (fallback_provider, fallback_model) in seen:
            continue
        if offline and fallback_provider != "openai_compatible":
            continue
        key, url = provider_credentials(config, fallback_provider)
        try:
            targets.append(JsonProvider(fallback_provider, key, fallback_model,
                                        output_cap(config, fallback_provider, max_tokens), directory,
                                        base_url=url, recorder=recorder, thinking_budget=thinking))
        except (ProviderFailure, ImportError) as error:
            logger.warning("Skipping fallback %s:%s (%s).", fallback_provider, fallback_model,
                           getattr(error, "code", type(error).__name__))
            continue
        seen.add((fallback_provider, fallback_model))
    return FallbackJsonProvider(targets) if len(targets) > 1 else targets[0]

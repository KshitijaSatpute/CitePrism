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


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ProviderFailure(RuntimeError):
    def __init__(self, code, provider, *, status_code=None, attempts=None):
        self.code = code
        self.provider = provider
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = code in TRANSIENT_ERRORS
        actions = {"authentication": "Check the configured API key and access permissions.",
                   "service_unavailable": "The service is temporarily unavailable (HTTP 503). Wait a few minutes, then retry the audit.",
                   "model_unavailable": "Check the configured model's availability for this account.",
                   "rate_limit": "Provider quota or rate limit reached; retry later.",
                   "timeout": "Provider timed out; retry this stage.",
                   "invalid_request": "The provider rejected the request configuration.",
                   "empty_response": "The provider returned no usable text.",
                   "provider_error": "The provider could not complete this request."}
        super().__init__(f"{provider}: {actions.get(code, actions['provider_error'])}")


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

    def __init__(self, provider, api_key, model, max_tokens, directory=None):
        self.provider, self.model, self.max_tokens = provider, model, max_tokens
        self.recorder = CallRecorder(directory)
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
            invoke = lambda: self.client.models.generate_content(
                model=self.model, contents=prompt,
                config={"temperature": 0.1, "max_output_tokens": self.max_tokens,
                        "response_mime_type": "application/json",
                        "automatic_function_calling": {"disable": True}})
            decode = lambda response: (response.text, usage_dict(response.usage_metadata))
        else:
            invoke = lambda: self.client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, max_tokens=self.max_tokens, temperature=0.1)
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

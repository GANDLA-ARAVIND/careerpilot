"""Thin LLM provider interface (CLAUDE.md: "All LLM calls go through one
LLMClient interface so providers can be swapped by config"). GeminiClient is
the only implementation for now - Groq stays unbuilt until actually needed.
"""

import os
import time
from abc import ABC, abstractmethod
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0
TIMEOUT_SECONDS = 60


class LLMError(Exception):
    """Raised when a completion fails after exhausting retries, or a
    provider returns something the caller can't use."""


class LLMClient(ABC):
    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifies which model this client talks to. Part of what
        determines a completion's output - callers (agents/analyst.py's
        cache key) use this alongside the prompt and input text, so two
        models analyzing the same job don't collide under one cache entry."""

    @abstractmethod
    def complete(self, system_instruction: str, user_message: str, response_schema: dict) -> str:
        """Send a prompt, constrained to response_schema where the provider
        supports native structured output. Returns raw text for the caller
        to parse and validate - this interface's job is getting back text
        that SHOULD conform to the schema, not returning a validated object.
        Validation stays the caller's responsibility so it behaves
        identically no matter which provider answered."""

    last_usage: Optional[dict] = None
    """Provider-reported token usage from the most recent complete() call,
    or None if the provider doesn't report it. Diagnostic only, not part of
    the core contract - shape is whatever the provider returns as-is, not
    normalized across providers, since only Gemini exists right now and
    normalizing for a hypothetical second provider would be speculative."""


def _post_with_backoff(url: str, json_body: dict, headers: dict) -> requests.Response:
    """POST-based exponential-backoff twin of adapters/base.py's
    request_with_backoff. Not shared code: ATS board-fetching (GET,
    404-is-a-config-error) and LLM completions (POST, 429-is-the-constraint-
    that-actually-matters on a free tier) have different retry semantics,
    and forcing one abstraction over both would be worse than the
    duplication. Same constants, same shape, so retry behavior stays
    recognizable across the codebase."""
    last_error: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_BASE_SECONDS * 2**(attempt - 1))
        try:
            response = requests.post(url, json=json_body, headers=headers, timeout=TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            continue

        if response.status_code == 429 or response.status_code >= 500:
            last_error = requests.HTTPError(f"{response.status_code} from {url}: {response.text[:200]}")
            continue
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise LLMError(f"Request to {url} failed: {exc}") from exc
        return response

    raise LLMError(f"Request to {url} failed after {MAX_ATTEMPTS} attempts: {last_error}") from last_error


class GeminiClient(LLMClient):
    """Talks to Gemini's generateContent REST endpoint directly - no SDK
    dependency, and it means this reuses the same backoff shape as the rest
    of the codebase instead of trusting an SDK's own, harder-to-inspect
    retry behavior."""

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, model: str, api_key: Optional[str] = None, requests_per_minute: Optional[float] = None):
        self.model = model
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY not set - copy .env.example to .env and fill it in")
        self.last_usage: Optional[dict] = None
        # Deliberate pacing, not just backoff-after-failure: config.py's
        # GEMINI_RATE_LIMITS gives each model's RPM ceiling, and the caller
        # (pipeline.py) passes it in here so every complete() call sleeps
        # just enough to stay under it before sending, rather than sending
        # as fast as possible and relying on 429s to slow down. Backoff
        # still exists in _post_with_backoff for genuine failures (5xx,
        # bursts this pacing doesn't fully prevent) - the two are
        # complementary, not redundant.
        self._min_interval_seconds = 60.0 / requests_per_minute if requests_per_minute else None
        self._last_call_started_at: Optional[float] = None
        # Every call attempted against this model, successful or not - a
        # request that gets rate-limited and retried still consumes real
        # quota, so this counts attempts, not just successes. Diagnostic
        # only, for pipeline.py's "call N/RPD" progress reporting.
        self.call_count = 0

    @property
    def model_name(self) -> str:
        return self.model

    def _pace(self) -> None:
        if self._min_interval_seconds is None:
            return
        now = time.monotonic()
        if self._last_call_started_at is not None:
            elapsed = now - self._last_call_started_at
            wait = self._min_interval_seconds - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_call_started_at = time.monotonic()

    def complete(self, system_instruction: str, user_message: str, response_schema: dict) -> str:
        self._pace()
        self.call_count += 1

        url = f"{self.BASE_URL}/{self.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": response_schema,
            },
        }
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

        response = _post_with_backoff(url, body, headers)
        payload = response.json()
        # usageMetadata is Gemini's own authoritative count - measured, not
        # estimated from a chars-per-token heuristic. Verified live:
        # totalTokenCount != promptTokenCount + candidatesTokenCount for
        # this model - it also includes thoughtsTokenCount, tokens spent on
        # internal reasoning that never appear in the visible output but do
        # count against quota. Budget from totalTokenCount, not from
        # prompt+candidates, or the real cost will look ~2x too low.
        self.last_usage = payload.get("usageMetadata")
        try:
            return payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected Gemini response shape: {payload}") from exc

    def count_tokens(self, system_instruction: str, user_message: str) -> int:
        """Gemini's countTokens endpoint - measures input tokens for a
        prompt without running generation, so it doesn't spend generateContent
        quota. Used to measure real per-job input cost across a whole batch
        cheaply, before committing free-tier generation quota to it."""
        url = f"{self.BASE_URL}/{self.model}:countTokens"
        body = {
            "generateContentRequest": {
                # required even though the model is already in the URL -
                # verified live: omitting it is a 400 INVALID_ARGUMENT
                # ("model is not specified"), not an optional nicety.
                "model": f"models/{self.model}",
                "systemInstruction": {"parts": [{"text": system_instruction}]},
                "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            }
        }
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

        response = _post_with_backoff(url, body, headers)
        payload = response.json()
        try:
            return payload["totalTokens"]
        except KeyError as exc:
            raise LLMError(f"Unexpected Gemini countTokens response shape: {payload}") from exc

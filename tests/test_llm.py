import json

import pytest
import requests

import llm
from llm import GeminiClient, LLMError, _post_with_backoff


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def _gemini_success_body(text, usage=None):
    body = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    if usage is not None:
        body["usageMetadata"] = usage
    return body


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiClient(model="gemini-2.5-flash", api_key=None)


def test_model_name_exposes_the_configured_model():
    client = GeminiClient(model="gemini-3.5-flash-lite", api_key="test-key")
    assert client.model_name == "gemini-3.5-flash-lite"


def test_complete_sends_correct_request_shape(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(200, _gemini_success_body('{"fit_score": 80}'))

    monkeypatch.setattr(requests, "post", fake_post)

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key")
    result = client.complete("system rules", "user content", {"type": "OBJECT"})

    assert result == '{"fit_score": 80}'
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert captured["json"]["systemInstruction"]["parts"][0]["text"] == "system rules"
    assert captured["json"]["contents"][0]["parts"][0]["text"] == "user content"
    assert captured["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert captured["json"]["generationConfig"]["responseSchema"] == {"type": "OBJECT"}
    assert captured["headers"]["x-goog-api-key"] == "test-key"
    # the key must never end up in the URL where it could land in logs
    assert "test-key" not in captured["url"]


def test_complete_increments_call_count_per_attempt(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return _FakeResponse(200, _gemini_success_body("ok"))

    monkeypatch.setattr(requests, "post", fake_post)

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key")
    assert client.call_count == 0

    client.complete("sys", "user", {})
    client.complete("sys", "user", {})

    assert client.call_count == 2


def test_complete_paces_calls_to_stay_under_requests_per_minute(monkeypatch):
    """requests_per_minute=30 means one call every 2 seconds. Fake the clock
    so this doesn't actually sleep in the test suite - the point is to
    verify the sleep duration requested, not to wait for it."""

    def fake_post(url, json, headers, timeout):
        return _FakeResponse(200, _gemini_success_body("ok"))

    monkeypatch.setattr(requests, "post", fake_post)

    fake_clock = {"t": 0.0}
    sleeps = []

    def fake_monotonic():
        return fake_clock["t"]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        fake_clock["t"] += seconds

    monkeypatch.setattr(llm.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(llm.time, "sleep", fake_sleep)

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key", requests_per_minute=30)

    client.complete("sys", "user", {})
    assert sleeps == []  # first call never waits - nothing to pace against yet

    fake_clock["t"] += 0.5  # simulate 0.5s of "real" work between calls
    client.complete("sys", "user", {})
    assert sleeps == [1.5]  # needed 2.0s total, 0.5s already elapsed


def test_complete_does_not_pace_when_requests_per_minute_is_none(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return _FakeResponse(200, _gemini_success_body("ok"))

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: pytest.fail("should not sleep"))

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key")
    client.complete("sys", "user", {})
    client.complete("sys", "user", {})  # no requests_per_minute set - no pacing


def test_complete_retries_on_429_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        if len(calls) < 3:
            return _FakeResponse(429, text="rate limited")
        return _FakeResponse(200, _gemini_success_body("ok"))

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)  # skip real backoff delay in tests

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key")
    result = client.complete("sys", "user", {})

    assert result == "ok"
    assert len(calls) == 3


def test_complete_raises_llm_error_after_exhausting_retries(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return _FakeResponse(429, text="rate limited")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key")
    with pytest.raises(LLMError):
        client.complete("sys", "user", {})


def test_complete_raises_on_unexpected_response_shape(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return _FakeResponse(200, {"unexpected": "shape"})

    monkeypatch.setattr(requests, "post", fake_post)

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key")
    with pytest.raises(LLMError, match="Unexpected Gemini response shape"):
        client.complete("sys", "user", {})


def test_complete_populates_last_usage_from_response(monkeypatch):
    usage = {"promptTokenCount": 500, "candidatesTokenCount": 120, "totalTokenCount": 620}

    def fake_post(url, json, headers, timeout):
        return _FakeResponse(200, _gemini_success_body("{}", usage=usage))

    monkeypatch.setattr(requests, "post", fake_post)

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key")
    assert client.last_usage is None  # nothing sent yet

    client.complete("sys", "user", {})
    assert client.last_usage == usage


def test_count_tokens_returns_total(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200, {"totalTokens": 742})

    monkeypatch.setattr(requests, "post", fake_post)

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key")
    total = client.count_tokens("sys", "user")

    assert total == 742
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:countTokens"
    assert captured["json"]["generateContentRequest"]["systemInstruction"]["parts"][0]["text"] == "sys"
    # regression check: Gemini 400s with "model is not specified" if this is
    # missing, even though the model is already in the URL - verified live
    assert captured["json"]["generateContentRequest"]["model"] == "models/gemini-2.5-flash"


def test_count_tokens_raises_on_unexpected_shape(monkeypatch):
    def fake_post(url, json, headers, timeout):
        return _FakeResponse(200, {"unexpected": "shape"})

    monkeypatch.setattr(requests, "post", fake_post)

    client = GeminiClient(model="gemini-2.5-flash", api_key="test-key")
    with pytest.raises(LLMError, match="Unexpected Gemini countTokens response shape"):
        client.count_tokens("sys", "user")


def test_post_with_backoff_retries_on_5xx_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(1)
        if len(calls) < 2:
            return _FakeResponse(503, text="overloaded")
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    response = _post_with_backoff("https://example.com", {}, {})
    assert response.status_code == 200
    assert len(calls) == 2

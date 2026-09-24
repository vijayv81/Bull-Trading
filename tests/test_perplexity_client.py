"""The Agent API response shape was confirmed live on 2026-09-24 (see
perplexity_client.py's module docstring) after a production incident where
the old-shape parsing silently returned empty extractions for every ticker.
These tests pin the real shape down so a future API change is caught here,
not discovered mid-checkpoint again.
"""

import pytest
import requests

from trading_agent.research import perplexity_client as pc


def _agent_response(text="the answer", urls=("http://a.example", "http://b.example")):
    return {
        "output": [
            {"type": "search_results", "queries": ["q1"], "results": [{"url": urls[0]}]},
            {"type": "search_results", "queries": ["q2"], "results": [{"url": urls[1]}]},
            {"type": "fetch_url_results", "contents": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        ]
    }


# --- _extract_text -------------------------------------------------------------


def test_extract_text_from_real_agent_api_shape():
    assert pc._extract_text(_agent_response(text="hello world")) == "hello world"


def test_extract_text_ignores_non_message_steps():
    raw = {
        "output": [
            {"type": "search_results", "queries": [], "results": []},
            {"type": "fetch_url_results", "contents": []},
        ]
    }
    assert pc._extract_text(raw) == ""


def test_extract_text_falls_back_to_legacy_output_text():
    assert pc._extract_text({"output_text": "legacy text"}) == "legacy text"


def test_extract_text_falls_back_to_legacy_chat_completions():
    raw = {"choices": [{"message": {"content": "chat completions text"}}]}
    assert pc._extract_text(raw) == "chat completions text"


def test_extract_text_empty_when_nothing_matches():
    assert pc._extract_text({}) == ""


# --- _extract_sources -----------------------------------------------------------


def test_extract_sources_from_search_results_steps():
    urls = pc._extract_sources(_agent_response(urls=("http://a.example", "http://b.example")))
    assert urls == ["http://a.example", "http://b.example"]


def test_extract_sources_empty_when_no_search_results():
    assert pc._extract_sources({"output": [{"type": "fetch_url_results", "contents": []}]}) == []


def test_extract_sources_falls_back_to_legacy_citations():
    assert pc._extract_sources({"citations": ["http://legacy.example"]}) == ["http://legacy.example"]


# --- query_perplexity retry behavior ---------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-fake")


def test_query_perplexity_succeeds_first_try(monkeypatch, api_key):
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    monkeypatch.setattr(pc.requests, "post", lambda *a, **k: _FakeResponse(200, {"ok": True}))
    assert pc.query_perplexity("prompt") == {"ok": True}


def test_query_perplexity_retries_on_timeout_then_succeeds(monkeypatch, api_key):
    calls = {"n": 0}
    sleeps = []
    monkeypatch.setattr(pc.time, "sleep", lambda s: sleeps.append(s))

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.Timeout("read timed out")
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(pc.requests, "post", flaky)
    assert pc.query_perplexity("prompt") == {"ok": True}
    assert calls["n"] == 2
    assert len(sleeps) == 1


def test_query_perplexity_raises_after_exhausting_retries(monkeypatch, api_key):
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)

    def always_times_out(*a, **k):
        raise requests.exceptions.Timeout("read timed out")

    monkeypatch.setattr(pc.requests, "post", always_times_out)
    with pytest.raises(requests.exceptions.Timeout):
        pc.query_perplexity("prompt")


def test_query_perplexity_retries_on_retryable_status(monkeypatch, api_key):
    calls = {"n": 0}
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(503)
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(pc.requests, "post", flaky)
    assert pc.query_perplexity("prompt") == {"ok": True}
    assert calls["n"] == 2


def test_query_perplexity_does_not_retry_non_retryable_4xx(monkeypatch, api_key):
    calls = {"n": 0}
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)

    def bad_request(*a, **k):
        calls["n"] += 1
        return _FakeResponse(400)

    monkeypatch.setattr(pc.requests, "post", bad_request)
    with pytest.raises(requests.exceptions.HTTPError):
        pc.query_perplexity("prompt")
    assert calls["n"] == 1

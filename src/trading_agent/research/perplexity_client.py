"""Perplexity AI research client (plan §4).

Turns raw news/sentiment into structured, citable JSON — never a final
decision. The raw response (including sources) is always persisted first;
the caller's extraction is a summary view over it, not the record of "why".

API surface note: Perplexity's Agent API (`/v1/agent`) response is a
Responses-API-style `output` array of heterogeneous steps — `search_results`
(one or more), `fetch_url_results`, then a final `message` step holding the
synthesized answer — confirmed against a live call on 2026-09-24. There is no
top-level `output_text` or `citations` field; both `_extract_text()` and
`_extract_sources()` walk `output` for the real shape. The legacy Chat
Completions fallback (`choices[].message.content` / `citations`) is kept for
whatever's still on that API ahead of its 2026-09-27 retirement.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from trading_agent.config import RAW_DATA_DIR, require_env
from trading_agent.utils import day_dir

PERPLEXITY_URL = "https://api.perplexity.ai/v1/agent"
CACHE_WINDOW_MINUTES = 30
REQUEST_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (3, 8)  # between attempt 1->2 and 2->3
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _raw_path(ticker: str, checkpoint: str) -> Path:
    return day_dir(RAW_DATA_DIR) / f"perplexity_{ticker}_{checkpoint}.json"


def _cached_extraction(ticker: str, checkpoint: str) -> dict[str, Any] | None:
    """Return a cached extraction if a same-checkpoint result exists and is fresh."""
    path = _raw_path(ticker, checkpoint)
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    fetched_at = datetime.fromisoformat(payload["fetched_at"])
    age_minutes = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 60
    if age_minutes > CACHE_WINDOW_MINUTES:
        return None
    return payload["extraction"]


def query_perplexity(prompt: str, preset: str = "low") -> dict[str, Any]:
    """Raw call to the Perplexity Agent API. Returns the parsed JSON response.

    The Agent API's search+fetch+synthesize pipeline runs multi-step server
    side and is genuinely slow under load — a hard timeout with no retry was
    observed killing a whole checkpoint mid-loop on one slow call. Retries up
    to MAX_ATTEMPTS total on a timeout/connection error or a retryable HTTP
    status, with a short backoff; anything else (a real 4xx, a bad prompt)
    raises immediately rather than retrying a failure that can't succeed.
    """
    api_key = require_env("PERPLEXITY_API_KEY")
    last_exc: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = requests.post(
                PERPLEXITY_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"preset": preset, "input": prompt},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            raise

    raise last_exc  # pragma: no cover - unreachable, loop always returns or raises


def _extract_text(raw: dict[str, Any]) -> str:
    """The assistant's final synthesized answer.

    Agent API: output is an ordered list of steps (search_results,
    fetch_url_results, ...) ending in a "message" step from the assistant;
    the text lives at that step's content[0].text. Walked in reverse since
    the message is always last but this doesn't assume a fixed position.
    Falls back to the legacy `output_text` / chat-completions shapes for
    whatever's still on the old API.
    """
    for item in reversed(raw.get("output") or []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for block in item.get("content") or []:
                if block.get("type") == "output_text" and block.get("text"):
                    return block["text"]
    if "output_text" in raw:
        return raw["output_text"]
    choices = raw.get("choices") or []
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def _extract_sources(raw: dict[str, Any]) -> list[str]:
    """Source URLs. Agent API: no top-level `citations` list anymore — pulled
    instead from every `search_results` step's `results[].url`. Falls back to
    the legacy `citations` field for whatever's still on the old API.
    """
    if raw.get("citations"):
        return raw["citations"]
    urls = []
    for item in raw.get("output") or []:
        if item.get("type") == "search_results":
            for result in item.get("results") or []:
                url = result.get("url")
                if url:
                    urls.append(url)
    return urls


def _perplexity_extraction(ticker: str, checkpoint: str, prompt: str) -> dict[str, Any] | None:
    """Perplexity attempt. Returns None (never raises past this point) when
    the call fails outright OR succeeds but synthesizes nothing usable (no
    text, no sources) — either way the caller falls back to
    _yahoo_fallback_extraction() rather than scoring on empty research.
    """
    try:
        raw = query_perplexity(prompt)
    except Exception as exc:
        print(f"Perplexity research failed for {ticker}: {exc} — trying the Yahoo Finance fallback.")
        return None

    text = _extract_text(raw)
    sources = _extract_sources(raw)
    if not text and not sources:
        print(f"Perplexity returned no usable content for {ticker} — trying the Yahoo Finance fallback.")
        return None

    return {
        "raw_response": raw,
        "extraction": {
            "ticker": ticker,
            "checkpoint": checkpoint,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "headline_summary": text,
            "sources": sources,
            "confidence_of_extraction": 1.0 if sources else 0.6,
            "research_source": "perplexity",
        },
    }


def _yahoo_fallback_extraction(ticker: str, checkpoint: str) -> dict[str, Any] | None:
    """Fallback when Perplexity is unavailable or empty (see module docstring
    and CLAUDE.md's credential policy — this needs no new credential, unlike
    a Google-backed fallback would, so it's the one wired up here). Returns
    None if Yahoo Finance also has nothing — the caller must then refuse to
    score this ticker at all rather than fabricate an analysis with no real
    research behind it.
    """
    from trading_agent.data.market_data import fetch_news_headlines

    headlines = fetch_news_headlines(ticker)
    if not headlines:
        return None

    sources = [h["url"] for h in headlines if h.get("url")]
    return {
        "raw_response": {"provider": "yahoo_finance_fallback", "headlines": headlines},
        "extraction": {
            "ticker": ticker,
            "checkpoint": checkpoint,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "headline_summary": " | ".join(h["title"] for h in headlines),
            "sources": sources,
            # Headlines alone, no synthesis — deliberately below Perplexity's
            # floor (0.6) so a fallback run never outscores a real one.
            "confidence_of_extraction": 0.5 if sources else 0.4,
            "research_source": "yahoo_finance_fallback",
        },
    }


def research_ticker(ticker: str, checkpoint: str, force_refresh: bool = False) -> dict[str, Any]:
    """Research a ticker's latest news/catalysts, caching per checkpoint.

    Returns a structured extraction:
    {ticker, timestamp, headline_summary, sources, confidence_of_extraction,
    research_source}. `catalysts` and `sentiment_score` are intentionally left
    for a follow-up pass (e.g. a scoring-time NLP or Claude call) rather than
    guessed here — see scoring/recommendation_engine.py for how the
    extraction is consumed.

    Perplexity is the primary source; if it's unreachable or returns nothing
    usable, this falls back to Yahoo Finance headlines (data/market_data.py's
    fetch_news_headlines() — free, no credential needed). If BOTH come back
    empty, this raises rather than returning a placeholder: scoring a ticker
    with no real research behind it — from either source — is exactly the
    failure mode this function exists to prevent, and the caller
    (orchestrator.run_checkpoint()) already treats an exception here as "skip
    this ticker," not "crash the checkpoint."
    """
    if not force_refresh:
        cached = _cached_extraction(ticker, checkpoint)
        if cached is not None:
            return cached

    prompt = (
        f"Latest news, catalysts, and analyst rating changes for {ticker} in the "
        "last 24 hours. Include any notable options flow or unusual volume commentary."
    )
    result = _perplexity_extraction(ticker, checkpoint, prompt) or _yahoo_fallback_extraction(ticker, checkpoint)
    if result is None:
        raise RuntimeError(
            f"No research data available for {ticker} — Perplexity returned nothing usable and "
            "the Yahoo Finance fallback found no recent headlines either. Refusing to score "
            f"{ticker} on no research data."
        )

    path = _raw_path(ticker, checkpoint)
    path.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "raw_response": result["raw_response"],
                "extraction": result["extraction"],
            },
            indent=2,
            default=str,
        )
    )
    return result["extraction"]


def screen_market(prompt_override: str | None = None) -> dict[str, Any]:
    """Broad screen used to *seed* dynamic candidates, never to auto-approve them
    (plan §4). Caller is responsible for corroborating any candidate against
    market data before it reaches scoring.
    """
    prompt = prompt_override or (
        "Which US equities are seeing unusual bullish momentum, analyst upgrades, "
        "or catalyst-driven moves today? List tickers with a one-line reason each."
    )
    raw = query_perplexity(prompt)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": _extract_text(raw),
        "sources": _extract_sources(raw),
    }

"""Perplexity AI research client (plan §4).

Turns raw news/sentiment into structured, citable JSON — never a final
decision. The raw response (including citations) is always persisted first;
the caller's extraction is a summary view over it, not the record of "why".

API surface note: as of this writing (2026-09-20), Perplexity is mid-migration
from "Sonar Chat Completions" to an "Agent API" (old endpoint's support window
closes 2026-09-27 per docs.perplexity.ai). This client targets the new Agent
API (`/v1/agent`). Verify against current docs before relying on this in
production — exact response field names were not independently confirmed
against a live call.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from trading_agent.config import RAW_DATA_DIR, require_env
from trading_agent.utils import day_dir

PERPLEXITY_URL = "https://api.perplexity.ai/v1/agent"
CACHE_WINDOW_MINUTES = 30


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
    """Raw call to the Perplexity Agent API. Returns the parsed JSON response."""
    api_key = require_env("PERPLEXITY_API_KEY")
    response = requests.post(
        PERPLEXITY_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"preset": preset, "input": prompt},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _extract_text(raw: dict[str, Any]) -> str:
    """Best-effort text extraction across the Agent API and legacy chat-completions shapes."""
    if "output_text" in raw:
        return raw["output_text"]
    choices = raw.get("choices") or []
    if choices:
        return choices[0].get("message", {}).get("content", "")
    return ""


def research_ticker(ticker: str, checkpoint: str, force_refresh: bool = False) -> dict[str, Any]:
    """Research a ticker's latest news/catalysts, caching per checkpoint.

    Returns a structured extraction:
    {ticker, timestamp, headline_summary, sources, confidence_of_extraction}.
    `catalysts` and `sentiment_score` are intentionally left for a follow-up
    pass (e.g. a scoring-time NLP or Claude call) rather than guessed here —
    see scoring/recommendation_engine.py for how the extraction is consumed.
    """
    if not force_refresh:
        cached = _cached_extraction(ticker, checkpoint)
        if cached is not None:
            return cached

    prompt = (
        f"Latest news, catalysts, and analyst rating changes for {ticker} in the "
        "last 24 hours. Include any notable options flow or unusual volume commentary."
    )
    raw = query_perplexity(prompt)

    extraction = {
        "ticker": ticker,
        "checkpoint": checkpoint,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "headline_summary": _extract_text(raw),
        "sources": raw.get("citations", []),
        "confidence_of_extraction": 1.0 if raw.get("citations") else 0.5,
    }

    path = _raw_path(ticker, checkpoint)
    path.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "raw_response": raw,
                "extraction": extraction,
            },
            indent=2,
            default=str,
        )
    )
    return extraction


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
        "sources": raw.get("citations", []),
    }

"""Secondary/backtest market data source (yfinance) — long-history OHLCV caching.

Separate from data/alpaca_client.py, which is the plan's primary source for
live quotes, bars, and order execution. This module exists for backtesting
(backtest/engine.py) and ad hoc `trading-agent ingest`, where a full year of
free daily history is more useful than Alpaca's recent-bars window.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from trading_agent.config import DATA_DIR, RAW_DATA_DIR

# Yahoo rate-limits this project's cloud IP on its cookie ("crumb") endpoints:
# on 2026-10-16..20 `Ticker.news` returned nothing ("Crumb fetch rate-limited
# (HTTP 429)") for every ticker, while Perplexity was also down (401), so no
# ticker could be scored at all. Three changes keep Yahoo usable:
#   1. Headlines come from Yahoo's public RSS feed, which needs no crumb and was
#      not rate-limited when the crumb endpoints were (Alpaca's news API, on the
#      Alpaca keys this project already has, is the next fallback, see
#      research/perplexity_client.py).
#   2. Sector and fundamentals (the crumb-based `.info`) are cached on disk in a
#      TRACKED file, data/reference/yahoo_reference.json, so a fresh routine
#      clone does not look every ticker up again: sector for 30 days,
#      fundamentals for 7. A failed lookup falls back to a stale cached value.
#   3. `.info` calls are spaced out (YAHOO_MIN_INTERVAL_S) and retried once
#      after a back-off when Yahoo says "too many requests".
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
REFERENCE_CACHE_PATH = DATA_DIR / "reference" / "yahoo_reference.json"
SECTOR_TTL_DAYS = 30
FUND_QUOTE_TYPES = {"ETF", "MUTUALFUND", "INDEX"}
FUNDAMENTALS_TTL_DAYS = 7
YAHOO_MIN_INTERVAL_S = 1.0
YAHOO_RETRY_BACKOFF_S = 5.0
_last_yahoo_call = 0.0


def fetch_price_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Download OHLCV history for a single ticker."""
    df = yf.Ticker(ticker).history(period=period, interval=interval)
    if df.empty:
        raise ValueError(f"No price data returned for {ticker!r}")
    df.index.name = "date"
    return df


def cache_price_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Fetch and persist price history to data/raw/{ticker}.parquet, returning the frame."""
    df = fetch_price_history(ticker, period=period, interval=interval)
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(RAW_DATA_DIR / f"{ticker.upper()}.parquet")
    return df


def load_cached_price_history(ticker: str) -> pd.DataFrame:
    """Load a previously cached frame; raises FileNotFoundError if never fetched."""
    path = RAW_DATA_DIR / f"{ticker.upper()}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"No cached data for {ticker!r} — run cache_price_history() first")
    return pd.read_parquet(path)


def fetch_watchlist(tickers: list[str], period: str = "1y", interval: str = "1d") -> dict[str, pd.DataFrame]:
    """Fetch and cache history for every ticker in a watchlist."""
    return {ticker: cache_price_history(ticker, period=period, interval=interval) for ticker in tickers}


def _parse_rss_items(xml_text: str, limit: int) -> list[dict[str, str]]:
    """Titles, links and dates from an RSS 2.0 body. Regex, not an XML parser:
    the feed is untrusted network input and only three plain fields are read."""
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml_text, flags=re.S)[:limit]:
        def field(name: str) -> str:
            m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", block, flags=re.S)
            return html.unescape(m.group(1).strip()) if m else ""

        title = field("title")
        if title:
            items.append({"title": title, "url": field("link"), "published": field("pubDate")})
    return items


def fetch_news_headlines(ticker: str, limit: int = 5) -> list[dict[str, str]]:
    """Recent Yahoo Finance headlines for `ticker` from the public RSS feed — a
    free, keyless secondary research source, used by
    research.perplexity_client.research_ticker() only as a fallback when
    Perplexity is unreachable or returns no usable content (plan §7.1: no new
    credential).

    The RSS feed replaced yfinance's `Ticker.news`, which goes through Yahoo's
    cookie ("crumb") endpoint and was rate-limited (HTTP 429) for this cloud IP
    on every call from 2026-10-16. Returns [] (never raises) on any failure or
    an empty feed; the caller treats that as "this source found nothing".
    """
    import requests

    try:
        resp = requests.get(
            YAHOO_RSS_URL,
            params={"s": ticker.upper(), "region": "US", "lang": "en-US"},
            headers={"User-Agent": "Mozilla/5.0 (Bull-Trading research fallback)"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return _parse_rss_items(resp.text, limit)
    except Exception:
        return []


def _load_reference() -> dict:
    try:
        return json.loads(REFERENCE_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_reference(ref: dict) -> None:
    try:
        REFERENCE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        REFERENCE_CACHE_PATH.write_text(json.dumps(ref, indent=1, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - a cache write must never break a lookup
        print(f"Could not write {REFERENCE_CACHE_PATH}: {exc}")


def _cached(kind: str, ticker: str, ttl_days: int) -> tuple[bool, object, bool]:
    """(present, value, fresh) for a reference-cache entry."""
    entry = _load_reference().get(kind, {}).get(ticker)
    if not entry:
        return False, None, False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(entry["at"])
    except Exception:
        return True, entry.get("value"), False
    return True, entry.get("value"), age <= timedelta(days=ttl_days)


def _store(kind: str, ticker: str, value: object) -> None:
    ref = _load_reference()
    ref.setdefault(kind, {})[ticker] = {"value": value, "at": datetime.now(timezone.utc).isoformat()}
    _save_reference(ref)


def _yahoo_info(ticker: str) -> dict:
    """yfinance `.info`, spaced out and retried once on a rate limit. Raises on failure."""
    global _last_yahoo_call
    for attempt in (1, 2):
        wait = YAHOO_MIN_INTERVAL_S - (time.monotonic() - _last_yahoo_call)
        if wait > 0:
            time.sleep(wait)
        _last_yahoo_call = time.monotonic()
        try:
            info = yf.Ticker(ticker).info or {}
            if info:
                return info
            raise RuntimeError("empty info")
        except Exception as exc:
            limited = any(s in str(exc).lower() for s in ("too many", "429", "rate"))
            if attempt == 2 or not limited:
                raise
            time.sleep(YAHOO_RETRY_BACKOFF_S)
    raise RuntimeError("unreachable")


_SECTOR_CACHE: dict[str, str | None] = {}


def get_sector(ticker: str) -> str | None:
    """GICS-style sector from yfinance's own classification (free, keyless,
    no new vendor — same policy reasoning as fetch_news_headlines() above),
    used by guardrails.sector_concentration_reason() to enforce
    risk_limits.yaml -> portfolio.max_sector_concentration_pct.

    None when yfinance has no sector for this symbol — routine for ETFs
    (e.g. SPY) and sometimes for newer/foreign listings — or on any lookup
    failure. Cached in-process for the life of the run: sector
    classification doesn't change intraday, and a BUY guardrail check may
    look this up for every currently-held position on top of the candidate
    itself, so repeat lookups within one checkpoint/order-submission should
    cost one network call per symbol, not one per check.
    """
    ticker = ticker.upper()
    if ticker not in _SECTOR_CACHE:
        present, value, fresh = _cached("sector", ticker, SECTOR_TTL_DAYS)
        if present and fresh:
            _SECTOR_CACHE[ticker] = value
        else:
            try:
                info = _yahoo_info(ticker)
                sector = info.get("sector") or None
                # No sector is only an answer for a fund (SPY, QQQ ...). For anything
                # else it is a partial response: on 2026-10 AAPL, CVX and V came back
                # without one, were cached as "no sector" for 30 days, and every BUY of
                # them was refused as "Unclassified". Not cached; retried next time.
                if sector is None and str(info.get("quoteType", "")).upper() not in FUND_QUOTE_TYPES:
                    raise RuntimeError(f"Yahoo returned no sector for {ticker}")
                _SECTOR_CACHE[ticker] = sector
                _store("sector", ticker, sector)
            except Exception:
                # stale beats nothing, but a stale "no sector" is not worth keeping
                _SECTOR_CACHE[ticker] = value if present else None
    return _SECTOR_CACHE[ticker]


_FUNDAMENTALS_CACHE: dict[str, dict | None] = {}


def get_fundamentals(ticker: str) -> dict | None:
    """The yfinance .info figures scoring/fundamentals.py reads (free, keyless,
    same policy reasoning as get_sector()/fetch_news_headlines()). None on any
    lookup failure. Cached in-process: fundamentals move quarterly, and one
    checkpoint can score the same ticker in two passes."""
    ticker = ticker.upper()
    if ticker not in _FUNDAMENTALS_CACHE:
        present, value, fresh = _cached("fundamentals", ticker, FUNDAMENTALS_TTL_DAYS)
        if present and fresh:
            _FUNDAMENTALS_CACHE[ticker] = value
        else:
            try:
                info = _yahoo_info(ticker)
                keep = (
                    "profitMargins", "revenueGrowth", "returnOnEquity", "debtToEquity",
                    "forwardPE", "trailingPE", "totalRevenue", "marketCap",
                )
                _FUNDAMENTALS_CACHE[ticker] = {k: info.get(k) for k in keep}
                _store("fundamentals", ticker, _FUNDAMENTALS_CACHE[ticker])
            except Exception:
                _FUNDAMENTALS_CACHE[ticker] = value if present else None  # stale beats nothing
    return _FUNDAMENTALS_CACHE[ticker]

"""Secondary/backtest market data source (yfinance) — long-history OHLCV caching.

Separate from data/alpaca_client.py, which is the plan's primary source for
live quotes, bars, and order execution. This module exists for backtesting
(backtest/engine.py) and ad hoc `trading-agent ingest`, where a full year of
free daily history is more useful than Alpaca's recent-bars window.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from trading_agent.config import RAW_DATA_DIR


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


def fetch_news_headlines(ticker: str, limit: int = 5) -> list[dict[str, str]]:
    """Recent news headlines from Yahoo Finance (yfinance) — a free, keyless
    secondary research source, used by research.perplexity_client.research_ticker()
    only as a fallback when Perplexity's Agent API is unreachable or returns no
    usable content (plan §7.1 credential policy: no new credential for a second
    vendor was introduced here on purpose — this needs none).

    Never the primary source: Perplexity's Agent API synthesizes analyst
    rating changes and options-flow commentary this doesn't attempt to
    replicate; this only surfaces raw headlines + links for the fallback to
    summarize. Returns [] (never raises) on any yfinance failure or an empty
    result — the caller treats an empty list as "this source found nothing
    either," not as an error of its own.
    """
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []

    headlines = []
    for item in items[:limit]:
        # yfinance's news payload shape has shifted between versions (a flat
        # {title, link, ...} dict vs. a nested {"content": {"title": ..., "canonicalUrl": {"url": ...}}}
        # one) — handle both rather than assume the current shape holds.
        content = item.get("content", item) if isinstance(item, dict) else {}
        title = content.get("title")
        url = content.get("link") or (content.get("canonicalUrl") or {}).get("url") or ""
        if title:
            headlines.append({"title": title, "url": url})
    return headlines


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
        try:
            _SECTOR_CACHE[ticker] = yf.Ticker(ticker).info.get("sector") or None
        except Exception:
            _SECTOR_CACHE[ticker] = None
    return _SECTOR_CACHE[ticker]

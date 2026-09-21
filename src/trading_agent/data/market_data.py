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

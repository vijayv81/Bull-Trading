"""Turns price history into a signal and a plain-English recommendation record.

The moving-average crossover below is a placeholder starting strategy —
swap in whatever signal logic your research agent produces.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

Signal = Literal["buy", "sell", "hold"]


def moving_average_signal(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> Signal:
    """Simple fast/slow SMA crossover on the 'Close' column."""
    if len(df) < slow:
        return "hold"
    fast_ma = df["Close"].rolling(fast).mean()
    slow_ma = df["Close"].rolling(slow).mean()
    prev_diff = fast_ma.iloc[-2] - slow_ma.iloc[-2]
    curr_diff = fast_ma.iloc[-1] - slow_ma.iloc[-1]
    if prev_diff <= 0 < curr_diff:
        return "buy"
    if prev_diff >= 0 > curr_diff:
        return "sell"
    return "hold"


def build_recommendation(ticker: str, df: pd.DataFrame) -> dict:
    """Package a signal plus supporting stats for the agent to narrate or a caller to log."""
    signal = moving_average_signal(df)
    close = df["Close"]
    return {
        "ticker": ticker,
        "signal": signal,
        "last_close": float(close.iloc[-1]),
        "pct_change_1d": float(close.pct_change().iloc[-1] * 100),
        "pct_change_20d": float(close.pct_change(20).iloc[-1] * 100) if len(close) > 20 else None,
        "sma_20": float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None,
        "sma_50": float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None,
        "note": "Research-only output, not investment advice.",
    }

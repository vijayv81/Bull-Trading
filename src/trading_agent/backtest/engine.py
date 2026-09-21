"""Vectorized backtest for the moving-average crossover strategy.

Swap the position logic here for whatever strategy you're testing —
the engine just needs a Series of daily returns to score.
"""

from __future__ import annotations

import pandas as pd

from trading_agent.backtest.metrics import cagr, max_drawdown, sharpe_ratio


def backtest_moving_average(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> dict:
    """Long when fast SMA > slow SMA, flat otherwise. Position lags signal by one day."""
    close = df["Close"]
    fast_ma = close.rolling(fast).mean()
    slow_ma = close.rolling(slow).mean()

    position = (fast_ma > slow_ma).astype(int).shift(1).fillna(0)
    daily_return = close.pct_change().fillna(0)
    strategy_return = position * daily_return

    return {
        "cagr": cagr(strategy_return),
        "sharpe": sharpe_ratio(strategy_return),
        "max_drawdown": max_drawdown(strategy_return),
        "buy_and_hold_cagr": cagr(daily_return),
        "num_trades": int(position.diff().abs().sum()),
    }

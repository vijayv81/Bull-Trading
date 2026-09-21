"""Standard performance metrics for a returns series."""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def cagr(returns: pd.Series) -> float:
    equity = (1 + returns).cumprod()
    years = len(returns) / TRADING_DAYS_PER_YEAR
    if years <= 0 or equity.iloc[-1] <= 0:
        return 0.0
    return float(equity.iloc[-1] ** (1 / years) - 1)


def sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.0) -> float:
    excess = returns - risk_free_rate / TRADING_DAYS_PER_YEAR
    if excess.std() == 0:
        return 0.0
    return float(np.sqrt(TRADING_DAYS_PER_YEAR) * excess.mean() / excess.std())


def max_drawdown(returns: pd.Series) -> float:
    equity = (1 + returns).cumprod()
    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    return float(drawdown.min())

"""Backtests for this project's technical strategy.

Swap the position logic here for whatever strategy you're testing —
the engine just needs a Series of daily returns to score.
"""

from __future__ import annotations

import pandas as pd

from trading_agent.backtest.metrics import cagr, max_drawdown, sharpe_ratio


def backtest_moving_average(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> dict:
    """Long when fast SMA > slow SMA, flat otherwise. Position lags signal by one day.

    A standalone reimplementation of the crossover idea, not the actual
    scoring.recommendation_engine functions the live pipeline calls — kept
    for a simple, fast comparison baseline. See backtest_strategy() below
    for a backtest that walks the real, deployed functions instead.
    """
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


def backtest_strategy(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> dict:
    """Walks the SAME scoring.recommendation_engine.technical_score() /
    score_candidate() functions the live pipeline calls through historical
    daily bars — unlike backtest_moving_average() above, which reimplements
    a standalone crossover that was never actually wired into the live
    confidence formula. Simulates one position at a time (BUY opens, SELL
    closes), tracking a cost basis so the stop-loss/take-profit
    position_pnl_pct bias (recommendation_engine.position_sell_pressure())
    gets exercised too, not just the raw technical direction in isolation.

    Disclosed limitation, not a shortcut: sentiment/catalyst/fundamental/
    historical_hitrate are always passed as None here, since there's no
    historical archive of Perplexity/Yahoo research text or journaled
    outcomes for past dates — the live sentiment/catalyst components are
    keyword-derived from research text that simply doesn't exist for
    historical dates. This can only validate the technical-driven,
    position-aware half of the live formula, which is still real progress
    over backtest_moving_average() never touching the deployed code at all.

    Reads config/risk_limits.yaml and config/agent_config.yaml directly
    (via score_candidate()) rather than taking them as parameters —
    deliberately: this answers "what would TODAY's configured strategy have
    done historically," so it stays honest as those files change instead of
    silently drifting from what's actually deployed.
    """
    from trading_agent.scoring.recommendation_engine import score_candidate, technical_score

    close = df["Close"]
    position_qty = 0.0
    cost_basis = 0.0
    daily_returns = []
    num_buys = 0
    num_sells = 0

    for i in range(slow, len(close)):
        window = df.iloc[: i + 1].rename(columns={"Close": "close"})
        price = float(close.iloc[i])
        prior_price = float(close.iloc[i - 1])

        tech = technical_score(window, fast=fast, slow=slow)
        position_pnl_pct = (
            (price - cost_basis) / cost_basis * 100 if position_qty > 0 and cost_basis > 0 else None
        )

        rec = score_candidate(
            "BACKTEST",
            "backtest",
            sentiment=None,
            technical=tech,
            fundamental=None,
            catalyst=None,
            position_pnl_pct=position_pnl_pct,
        )

        daily_returns.append((price - prior_price) / prior_price if position_qty > 0 else 0.0)

        if rec["action"] == "BUY" and position_qty == 0:
            position_qty, cost_basis = 1.0, price
            num_buys += 1
        elif rec["action"] == "SELL" and position_qty > 0:
            position_qty, cost_basis = 0.0, 0.0
            num_sells += 1

    returns = pd.Series(daily_returns)
    return {
        "cagr": cagr(returns),
        "sharpe": sharpe_ratio(returns),
        "max_drawdown": max_drawdown(returns),
        "buy_and_hold_cagr": cagr(close.pct_change().fillna(0)),
        "num_buys": num_buys,
        "num_sells": num_sells,
        "note": (
            "sentiment/catalyst/fundamental/historical_hitrate excluded — no historical "
            "research-text archive to derive them from; validates the technical + "
            "stop-loss/take-profit half of the live formula only."
        ),
    }

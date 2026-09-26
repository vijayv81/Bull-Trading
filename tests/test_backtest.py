import pandas as pd
import pytest

from trading_agent.backtest.engine import backtest_moving_average, backtest_strategy
from trading_agent.scoring import recommendation_engine as engine


def test_backtest_returns_expected_keys():
    closes = [100 + i * 0.5 for i in range(120)]
    df = pd.DataFrame({"Close": closes})
    result = backtest_moving_average(df, fast=5, slow=20)
    assert set(result.keys()) == {
        "cagr",
        "sharpe",
        "max_drawdown",
        "buy_and_hold_cagr",
        "num_trades",
    }


# --- backtest_strategy: walks the real technical_score()/score_candidate() -----


@pytest.fixture
def fixed_strategy_config(monkeypatch):
    monkeypatch.setattr(
        engine,
        "load_agent_config",
        lambda: {
            "scoring_weights": {
                "sentiment": 0.25, "technical": 0.30, "fundamental": 0.15,
                "catalyst": 0.20, "historical_hitrate": 0.10,
            }
        },
    )
    monkeypatch.setattr(
        engine,
        "load_risk_limits",
        lambda: {"position": {"min_confidence_to_notify": 50, "max_position_pct_of_portfolio": 5.0}},
    )
    monkeypatch.setattr(engine, "historical_hitrate", lambda ticker: None)


def test_backtest_strategy_returns_expected_keys(fixed_strategy_config):
    closes = [100 + i * 0.5 for i in range(120)]
    df = pd.DataFrame({"Close": closes})
    result = backtest_strategy(df, fast=5, slow=20)
    assert set(result.keys()) == {
        "cagr", "sharpe", "max_drawdown", "buy_and_hold_cagr", "num_buys", "num_sells", "note",
    }


def test_backtest_strategy_discloses_the_excluded_components(fixed_strategy_config):
    closes = [100 + i * 0.5 for i in range(120)]
    df = pd.DataFrame({"Close": closes})
    result = backtest_strategy(df, fast=5, slow=20)
    assert "sentiment" in result["note"] and "catalyst" in result["note"]


def test_backtest_strategy_buys_on_a_strong_uptrend(fixed_strategy_config):
    closes = [100 + i * 0.8 for i in range(120)]
    df = pd.DataFrame({"Close": closes})
    result = backtest_strategy(df, fast=5, slow=20)
    assert result["num_buys"] >= 1


def test_backtest_strategy_exits_on_a_sharp_crash_after_buying(fixed_strategy_config):
    # A steady uptrend (triggers a BUY), then a sharp multi-day crash — the
    # position must actually close out again (via the technical read
    # flipping and/or the stop-loss bias), not stay open forever.
    rise = [100 + i * 0.8 for i in range(80)]
    crash = [rise[-1] * (0.90 ** i) for i in range(1, 15)]
    df = pd.DataFrame({"Close": rise + crash})
    result = backtest_strategy(df, fast=5, slow=20)
    assert result["num_buys"] >= 1
    assert result["num_sells"] >= 1

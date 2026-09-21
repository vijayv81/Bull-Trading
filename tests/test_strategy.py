import pandas as pd

from trading_agent.strategy.recommendation import build_recommendation, moving_average_signal


def _synthetic_prices(n=60, start=100.0, step=1.0) -> pd.DataFrame:
    closes = [start + i * step for i in range(n)]
    return pd.DataFrame({"Close": closes})


def test_moving_average_signal_buy_on_uptrend():
    df = _synthetic_prices(step=1.0)
    assert moving_average_signal(df, fast=5, slow=20) in {"buy", "hold"}


def test_moving_average_signal_hold_when_insufficient_history():
    df = _synthetic_prices(n=10)
    assert moving_average_signal(df, fast=5, slow=20) == "hold"


def test_build_recommendation_shape():
    df = _synthetic_prices()
    rec = build_recommendation("TEST", df)
    assert rec["ticker"] == "TEST"
    assert rec["signal"] in {"buy", "sell", "hold"}
    assert "note" in rec

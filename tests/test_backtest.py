import pandas as pd

from trading_agent.backtest.engine import backtest_moving_average


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

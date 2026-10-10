"""scoring/timing.py: wait-or-proceed from the last week and month of prices."""

import pandas as pd

from trading_agent.scoring.timing import timing_check


def _bars(closes):
    return pd.DataFrame({"close": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes]})


def _climb():  # 25 flat bars at 10, then a run to 13: price at the top, far above the week average
    return _bars([10.0] * 20 + [10.5, 11.0, 11.5, 12.0, 12.5])


def test_buy_at_top_of_range_and_above_week_avg_waits():
    out = timing_check("BUY", 13.0, _climb())
    assert out["verdict"] == "wait"
    assert out["target_price"] == 11.5 and out["potential_improvement_pct"] > 3


def test_buy_in_the_middle_proceeds():
    out = timing_check("BUY", 10.5, _bars([10.0, 11.0] * 13))
    assert out["verdict"] == "proceed"


def test_sell_at_bottom_below_week_avg_waits():
    closes = [12.0] * 20 + [11.5, 11.0, 10.5, 10.0, 9.5]
    out = timing_check("SELL", 9.0, _bars(closes))
    assert out["verdict"] == "wait" and out["target_price"] < 11.0


def test_stop_loss_sell_is_never_deferred_by_default():
    closes = [12.0] * 20 + [11.5, 11.0, 10.5, 10.0, 9.5]
    out = timing_check("SELL", 9.0, _bars(closes), stop_loss_hit=True)
    assert out["verdict"] == "proceed" and "Stop-loss" in out["reason"]
    assert timing_check("SELL", 9.0, _bars(closes), stop_loss_hit=True, cfg={"defer_stop_loss_exits": True})["verdict"] == "wait"


def test_not_enough_history_or_disabled_is_no_opinion():
    assert timing_check("BUY", 13.0, _bars([10.0] * 10)) is None
    assert timing_check("BUY", 13.0, _climb(), cfg={"enabled": False}) is None
    assert timing_check("HOLD", 13.0, _climb()) is None

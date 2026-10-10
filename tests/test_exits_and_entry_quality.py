"""Exits are never held back by the order caps or the daily-loss halt, and a BUY
a 4% stop-loss cannot protect is refused (user instruction 2026-10-10, after the
week of 2026-10-05: stop-loss SELLs queued behind the 5-a-day cap, GRMLW sold at
-48%, and the sub-$3 names that were bought in the first place)."""

import json

import pandas as pd
import pytest

from trading_agent import guardrails as g
from trading_agent.execute import auto_pilot as ap
from trading_agent.execute import order_manager as om
from trading_agent.scoring import entry_quality as eq

EQ_CFG = {
    "enabled": True,
    "min_price": 2.0,
    "max_avg_daily_range_pct": 8.0,
    "range_lookback_bars": 20,
    "max_daily_move_risk_pct_of_portfolio": 0.30,
    "risky_price_below": 5.0,
    "risky_min_history_bars": 200,
    "risky_min_confidence_to_buy": 92,
}


def _bars(n, close=50.0, range_pct=2.0):
    half = close * range_pct / 200
    return pd.DataFrame(
        {"open": [close] * n, "high": [close + half] * n, "low": [close - half] * n, "close": [close] * n}
    )


# --- order caps: SELLs neither count nor are refused -------------------------


@pytest.fixture
def trades_today(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "TRADES_DIR", tmp_path)
    from trading_agent.utils import today

    path = tmp_path / today()
    path.mkdir(parents=True)

    def write(records):
        (path / "orders_submitted.json").write_text(json.dumps(records))

    return write


def _limits(**portfolio):
    return lambda: {"portfolio": {"max_daily_trades": 5, **portfolio}}


def _buys(n):
    return [{"recommendation": {"action": "BUY"}, "order": {"symbol": "X"}, "source": "auto"} for _ in range(n)]


def _sells(n):
    return [{"recommendation": {"action": "SELL"}, "order": {"symbol": "X"}, "source": "auto"} for _ in range(n)]


def test_is_exit_order_reads_action_then_side_and_defaults_to_entry():
    assert g.is_exit_order({"recommendation": {"action": "SELL"}})
    assert not g.is_exit_order({"recommendation": {"action": "BUY"}})
    assert g.is_exit_order({"order": {"side": "OrderSide.SELL"}})
    assert not g.is_exit_order({"order": {"symbol": "X"}})


def test_sell_passes_the_daily_cap_when_exits_are_exempt(monkeypatch, trades_today):
    monkeypatch.setattr(g, "load_risk_limits", _limits(exits_exempt_from_daily_trade_cap=True))
    trades_today(_buys(5))
    assert g.daily_trade_count_reason("SELL") is None
    assert "Daily trade cap reached" in g.daily_trade_count_reason("BUY")


def test_sells_do_not_use_up_the_cap_for_buys(monkeypatch, trades_today):
    monkeypatch.setattr(g, "load_risk_limits", _limits(exits_exempt_from_daily_trade_cap=True))
    trades_today(_sells(5) + _buys(2))
    assert g.daily_trade_count_reason("BUY") is None


def test_exempt_off_restores_the_old_cap_for_sells_too(monkeypatch, trades_today):
    monkeypatch.setattr(g, "load_risk_limits", _limits(exits_exempt_from_daily_trade_cap=False))
    trades_today(_sells(3) + _buys(2))
    assert "Daily trade cap reached" in g.daily_trade_count_reason("SELL")


# --- order_manager: halt and entry quality -----------------------------------


def _rec(action="SELL"):
    return {"ticker": "GRMLW", "checkpoint": "pre_open", "action": action, "confidence": 90.0}


@pytest.fixture
def gate(monkeypatch):
    """Everything passes except the pieces under test."""
    monkeypatch.setattr(om, "load_risk_limits", lambda: {"operational": {"trading_enabled": True}})
    monkeypatch.setattr(om, "min_confidence_reason", lambda *a: None)
    monkeypatch.setattr(om, "entry_quality_reason", lambda rec: None)
    monkeypatch.setattr(om, "daily_trade_count_reason", lambda side=None: None)
    monkeypatch.setattr(
        om, "get_decision",
        lambda t, c: {"decision": "approve", "timestamp": "2026-10-12T12:00:00+00:00", "terms": {"qty": 10}},
    )
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(om, "stale_recommendation_reason", lambda rec: None)
    monkeypatch.setattr(om, "entry_limit_price", lambda t, s: (None, None))
    monkeypatch.setattr(om, "instrument_reason", lambda t, s, p=None: None)
    monkeypatch.setattr(om, "position_size_reason", lambda *a, **k: None)
    monkeypatch.setattr(om, "position_count_reason", lambda *a: None)
    monkeypatch.setattr(om, "sector_concentration_reason", lambda *a: None)
    monkeypatch.setattr(om, "short_sale_reason", lambda *a: None)
    monkeypatch.setattr(om, "submit_market_order", lambda *a, **k: {"id": "o1"})
    monkeypatch.setattr(om, "append_json", lambda *a, **k: None)
    monkeypatch.setattr(om, "daily_loss_reason", lambda: "Daily loss -19.6% has reached the 2.0% cap")


def test_a_sell_goes_through_the_daily_loss_halt_when_exits_are_allowed(monkeypatch, gate):
    monkeypatch.setattr(om, "halt_allows_exits", lambda: True)
    assert om.submit_approved_order(_rec("SELL"), 10)["id"] == "o1"


def test_a_buy_is_still_refused_by_the_daily_loss_halt(monkeypatch, gate):
    monkeypatch.setattr(om, "halt_allows_exits", lambda: True)
    with pytest.raises(om.OrderRefused, match="Daily loss"):
        om.submit_approved_order(_rec("BUY"), 10)


def test_a_sell_is_refused_by_the_halt_when_exits_are_not_allowed(monkeypatch, gate):
    monkeypatch.setattr(om, "halt_allows_exits", lambda: False)
    with pytest.raises(om.OrderRefused, match="Daily loss"):
        om.submit_approved_order(_rec("SELL"), 10)


def test_entry_quality_breach_refuses_a_buy_at_the_gate(monkeypatch, gate):
    monkeypatch.setattr(om, "halt_allows_exits", lambda: False)
    monkeypatch.setattr(om, "daily_loss_reason", lambda: None)
    monkeypatch.setattr(om, "entry_quality_reason", lambda rec: "moves 40% a day")
    with pytest.raises(om.OrderRefused, match="moves 40%"):
        om.submit_approved_order(_rec("BUY"), 10)


# --- entry quality ------------------------------------------------------------


@pytest.fixture(autouse=True)
def eq_cfg(monkeypatch):
    monkeypatch.setattr(eq, "load_risk_limits", lambda: {"entry_quality": EQ_CFG})


def test_average_daily_range_over_the_lookback():
    assert eq.avg_daily_range_pct(_bars(30, close=50, range_pct=10)) == pytest.approx(10.0)
    assert eq.avg_daily_range_pct(_bars(10)) is None  # fewer bars than the lookback


def test_a_calm_established_name_passes_with_no_size_cap_binding():
    out = eq.assess_entry("GOOG", 340.0, _bars(250, close=340, range_pct=2.3), 88)
    assert out["blocked"] is None and not out["risky"]
    assert out["max_size_pct"] == pytest.approx(13.04, abs=0.01)  # 0.30 * 100 / 2.3


def test_a_wild_name_is_refused_even_at_100_confidence():
    out = eq.assess_entry("INLF", 6.0, _bars(250, close=6, range_pct=11.9), 100)
    assert "11.9% a day" in out["blocked"]


def test_a_sub_floor_price_is_refused():
    out = eq.assess_entry("SOAR", 0.31, _bars(250, close=0.31, range_pct=3), 100)
    assert "under the $2 entry floor" in out["blocked"]


def test_a_thin_record_needs_the_higher_confidence_bar():
    short = _bars(100, close=40, range_pct=2)  # under 200 bars
    assert "needs confidence 92" in eq.assess_entry("NEWCO", 40.0, short, 88)["blocked"]
    assert eq.assess_entry("NEWCO", 40.0, short, 93)["blocked"] is None
    cheap = _bars(250, close=4, range_pct=2)  # priced under $5
    assert "needs confidence 92" in eq.assess_entry("CHEAP", 4.0, cheap, 90)["blocked"]


def test_size_is_bounded_so_a_normal_day_costs_a_set_share():
    out = eq.assess_entry("MID", 30.0, _bars(250, close=30, range_pct=7.5), 95)
    assert out["blocked"] is None
    assert out["max_size_pct"] == pytest.approx(4.0)  # 0.30 * 100 / 7.5


def test_disabled_gives_no_reading(monkeypatch):
    monkeypatch.setattr(eq, "load_risk_limits", lambda: {"entry_quality": {**EQ_CFG, "enabled": False}})
    assert eq.assess_entry("X", 1.0, _bars(250, close=1, range_pct=50), 50) is None


def test_the_order_time_backstop_reads_the_recommendation_fields(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"entry_quality": EQ_CFG})
    base = {"ticker": "GRMLW", "action": "BUY", "confidence": 100, "history_bars": 250}
    assert "entry floor" in g.entry_quality_reason({**base, "reference_price": 0.45, "avg_daily_range_pct": 38})
    assert "a day on average" in g.entry_quality_reason({**base, "reference_price": 9.0, "avg_daily_range_pct": 38})
    # a SELL is never blocked, and a record without the fields skips just those rules
    assert g.entry_quality_reason({**base, "action": "SELL", "reference_price": 0.45}) is None
    assert g.entry_quality_reason({"ticker": "OLD", "action": "BUY", "confidence": 95}) is None


# --- auto-apply: exits are exempt from the daily cap and go first ---------------


def _auto_rec(ticker, action, confidence):
    return {
        "ticker": ticker, "checkpoint": "pre_open", "action": action,
        "confidence": confidence, "suggested_size_pct_of_portfolio": 2.0,
    }


@pytest.fixture
def auto(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "TRADES_DIR", tmp_path / "trades")
    monkeypatch.setattr(g, "TRADES_DIR", tmp_path / "trades")
    monkeypatch.setattr(
        ap, "load_risk_limits",
        lambda: {"operational": {"auto_apply": {"enabled": True, "max_trades_per_day": 2}}},
    )
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"portfolio": {"exits_exempt_from_daily_trade_cap": True}})
    monkeypatch.setattr("trading_agent.journal.record_entry", lambda *a, **k: None)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_positions", lambda: [])
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"})
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "10"})
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_held_qty", lambda t: 10_000.0)
    placed = []
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: placed.append(rec["ticker"]) or {"id": "x"})
    return placed


def _used_up(tmp_path, n=2):
    from trading_agent.utils import append_json, day_dir

    for _ in range(n):
        append_json(
            day_dir(ap.TRADES_DIR) / "orders_submitted.json",
            {"source": "auto", "recommendation": {"action": "BUY"}, "order": {}},
        )


def test_stop_loss_sells_still_go_out_after_the_daily_cap_is_used_up(auto, tmp_path):
    _used_up(tmp_path)
    results = ap.auto_apply([_auto_rec("GRMLW", "SELL", 60), _auto_rec("NEWBUY", "BUY", 99)], "pre_open")
    assert auto == ["GRMLW"]  # the SELL went; the BUY did not
    assert [r["ticker"] for r in results] == ["GRMLW"]
    logged = json.loads((ap.TRADES_DIR / ap.today() / "auto_apply_attempts_pre_open.json").read_text())
    assert any(a["ticker"] == "NEWBUY" and a["status"] == "capped" for a in logged)


def test_exits_are_tried_before_higher_confidence_buys_and_do_not_use_a_slot(auto):
    recs = [
        _auto_rec("BUY1", "BUY", 99), _auto_rec("BUY2", "BUY", 98), _auto_rec("BUY3", "BUY", 97),
        _auto_rec("EXIT1", "SELL", 40), _auto_rec("EXIT2", "SELL", 30), _auto_rec("EXIT3", "SELL", 20),
    ]
    ap.auto_apply(recs, "midday")
    assert auto[:3] == ["EXIT1", "EXIT2", "EXIT3"]  # exits first
    assert auto[3:] == ["BUY1", "BUY2"]  # all three exits placed, the cap of 2 still left for two BUYs


def test_sells_already_filled_today_do_not_count_toward_the_auto_cap(auto, tmp_path):
    from trading_agent.utils import append_json, day_dir

    for _ in range(4):
        append_json(
            day_dir(ap.TRADES_DIR) / "orders_submitted.json",
            {"source": "auto", "recommendation": {"action": "SELL"}, "order": {}},
        )
    assert ap._todays_auto_trade_count() == 0
    ap.auto_apply([_auto_rec("BUY1", "BUY", 95)], "midday")
    assert auto == ["BUY1"]

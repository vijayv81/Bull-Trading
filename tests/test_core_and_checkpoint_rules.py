"""Exit sizing, the pre_open BUY block and the core-holding/satellite split
(user instruction 2026-10-12, after the 10-12 session: ALISU sold in slivers of
29/28/7 shares while falling 65%, every pre_open BUY measured had lost, and the
account sat 91% in cash)."""

import pytest

from trading_agent import guardrails as g
from trading_agent import orchestrator as orch
from trading_agent.execute import auto_pilot as ap
from trading_agent.execute import core_holding as ch

CORE = {
    "enabled": True,
    "symbol": "SPY",
    "checkpoint": "midday",
    "target_pct_of_portfolio": 70.0,
    "rebalance_band_pct": 5.0,
    "max_order_pct_of_portfolio": 10.0,
    "max_position_pct_of_portfolio": 75.0,
    "satellite_max_pct_of_portfolio": 20.0,
}


def _limits(**extra):
    return lambda: {
        "position": {"max_position_pct_of_portfolio": 5.0},
        "portfolio": {"max_sector_concentration_pct": 30.0},
        "execution": {"no_new_buys_at_checkpoints": ["pre_open"], "reverify_technical": True},
        "core": CORE,
        **extra,
    }


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", _limits())
    monkeypatch.setattr(ch, "core_cfg", g.core_cfg)


# --- no new BUYs at pre_open ----------------------------------------------------


def test_a_pre_open_buy_is_refused_but_other_checkpoints_and_sells_are_not():
    assert "pre_open" in g.buy_checkpoint_reason({"action": "BUY", "checkpoint": "pre_open"})
    assert g.buy_checkpoint_reason({"action": "BUY", "checkpoint": "market_open"}) is None
    assert g.buy_checkpoint_reason({"action": "BUY", "checkpoint": "midday"}) is None
    assert g.buy_checkpoint_reason({"action": "SELL", "checkpoint": "pre_open"}) is None
    assert g.buy_checkpoint_reason({"action": "BUY", "checkpoint": "core"}) is None
    assert g.buy_checkpoint_reason({"action": "BUY"}) is None  # no checkpoint on the record


def test_an_empty_list_is_the_revert(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", _limits(execution={"no_new_buys_at_checkpoints": []}))
    assert g.buy_checkpoint_reason({"action": "BUY", "checkpoint": "pre_open"}) is None


# --- stop-loss exits sell the whole position -------------------------------------


def _sell(pnl, pct=1.0, stop=4.0):
    return {"ticker": "ALISU", "action": "SELL", "position_pnl_pct": pnl, "stop_loss_pct": stop,
            "suggested_size_pct_of_portfolio": pct}


def test_stop_loss_detection():
    assert ap.is_stop_loss_exit(_sell(-47.6))
    assert ap.is_stop_loss_exit(_sell(-4.0))
    assert not ap.is_stop_loss_exit(_sell(-3.9))
    assert not ap.is_stop_loss_exit(_sell(8.5))  # a take-profit trim
    assert not ap.is_stop_loss_exit({**_sell(-50), "action": "BUY"})
    assert not ap.is_stop_loss_exit({"action": "SELL"})  # no P&L on the record


def test_a_stop_loss_exit_sells_everything_held_not_the_suggested_slice(monkeypatch):
    monkeypatch.setattr(ap, "_held_qty", lambda t: 64.0)
    # 1% of 73,000 at $17.21 is 42 shares; the whole position is 64
    assert ap.qty_for_recommendation(_sell(-47.6), equity=73_000.0, price=17.21) == 64.0
    # ... and it does not need a price
    assert ap.qty_for_recommendation(_sell(-47.6), equity=73_000.0, price=0.0) == 64.0


def test_a_take_profit_trim_keeps_its_suggested_size(monkeypatch):
    monkeypatch.setattr(ap, "_held_qty", lambda t: 2266.0)
    rec = {**_sell(8.5, pct=3.4), "ticker": "MGLD"}
    assert ap.qty_for_recommendation(rec, equity=76_000.0, price=2.0) == 1292.0  # 3.4% of equity


def test_a_unit_sell_is_not_capped_at_the_unit_buy_size(monkeypatch):
    import pandas as pd

    monkeypatch.setattr(orch, "load_watchlist", lambda: ["ALISU"])
    monkeypatch.setattr(orch, "get_market_movers", lambda: {"gainers": []})
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "ALISU", "unrealized_plpc": -0.4}])
    monkeypatch.setattr(orch, "get_fundamentals", lambda t: None)
    monkeypatch.setattr(orch, "get_recent_bars", lambda t, **k: [])
    monkeypatch.setattr(orch, "get_mid_price", lambda t: None)
    monkeypatch.setattr(orch, "get_market_return_pct", lambda s: None)
    monkeypatch.setattr(orch, "technical_score", lambda b: 0.5)
    monkeypatch.setattr(orch, "save_recommendation", lambda r: None)
    monkeypatch.setattr(orch, "auto_apply", lambda recs, cp: [])
    monkeypatch.setattr(orch, "daily_loss_reason", lambda: None)
    monkeypatch.setattr(orch, "assess_entry", lambda *a, **k: None)
    monkeypatch.setattr(orch, "core_rebalance", lambda cp: [])
    monkeypatch.setattr(orch, "notify_digest", lambda *a, **k: None)
    monkeypatch.setattr(orch, "research_ticker", lambda t, c: {"headline_summary": "x", "sources": ["s"]})
    monkeypatch.setattr(
        orch, "score_candidate",
        lambda **kw: {"ticker": kw["ticker"], "checkpoint": kw["checkpoint"], "action": "SELL",
                      "confidence": 85, "suggested_size_pct_of_portfolio": 4.0},
    )
    rec = orch.run_checkpoint("midday")[0]
    assert rec["instrument_class"] == "blank_check_unit"
    assert rec["suggested_size_pct_of_portfolio"] == 4.0  # a SELL keeps its size


# --- guardrails: the core's own cap, sector exemption, satellite cap -------------


@pytest.fixture
def account(monkeypatch):
    positions = [
        {"symbol": "SPY", "market_value": "30000", "qty": "40"},
        {"symbol": "TSLA", "market_value": "4000", "qty": "10"},
        {"symbol": "MGLD", "market_value": "2000", "qty": "1000"},
    ]
    monkeypatch.setattr(g, "_account", lambda: {"equity": "100000", "cash": "64000"})
    monkeypatch.setattr(g, "_positions", lambda: positions)
    monkeypatch.setattr(g, "_reference_price", lambda s: 100.0)
    return positions


def test_the_core_symbol_has_its_own_position_cap(account):
    # 30,000 held + 40,000 more = 70% of 100,000: over the normal 5% cap, under the core's 75%
    assert g.position_size_reason("SPY", "BUY", 400, price=100.0) is None
    assert "over the 75" in g.position_size_reason("SPY", "BUY", 500, price=100.0)
    assert "over the 5.0% cap" in g.position_size_reason("TSLA", "BUY", 100, price=100.0)


def test_the_core_is_not_a_sector_bet(account, monkeypatch):
    monkeypatch.setattr(g, "_sector_bucket", lambda s: "Unclassified")
    assert g.sector_concentration_reason("SPY", "BUY", 400) is None
    assert g.sector_concentration_reason("MGLD", "BUY", 400) is not None  # 30k SPY + new > 30% Unclassified


def test_satellite_cap_counts_everything_but_the_core(account):
    # satellite now 6,000 (6%); +10,000 more is 16% < 20%
    assert g.satellite_exposure_reason("NVDA", "BUY", 100, price=100.0) is None
    # +16,000 would be 22%
    reason = g.satellite_exposure_reason("NVDA", "BUY", 160, price=100.0)
    assert "satellite cap" in reason and "22.0%" in reason
    assert g.satellite_exposure_reason("NVDA", "SELL", 160, price=100.0) is None
    assert g.satellite_exposure_reason("SPY", "BUY", 160, price=100.0) is None  # the core is exempt


def test_satellite_cap_fails_closed_on_an_unreadable_account(monkeypatch):
    def boom():
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(g, "_account", boom)
    assert "refusing to proceed blind" in g.satellite_exposure_reason("NVDA", "BUY", 1, price=10.0)


def test_core_off_means_no_special_rules(monkeypatch, account):
    monkeypatch.setattr(g, "load_risk_limits", _limits(core={"enabled": False, "symbol": "SPY"}))
    assert g.core_symbol() is None
    assert g.satellite_exposure_reason("NVDA", "BUY", 100000, price=100.0) is None
    assert "5.0% cap" in g.position_size_reason("SPY", "BUY", 400, price=100.0)


def test_the_core_skips_the_technical_direction_recheck_but_a_satellite_name_does_not(monkeypatch):
    import pandas as pd

    monkeypatch.setattr(g, "load_risk_limits", _limits(execution={"max_price_drift_pct": 4.0, "reverify_technical": True}))
    monkeypatch.setattr(g, "_mid_price", lambda s: 100.0)
    bars = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}] * 80  # drives a flat/low technical
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars", lambda s, **k: bars)
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.technical_score", lambda b: 0.1)
    core_rec = {"ticker": "SPY", "action": "BUY", "reference_price": 100.0, "core": True}
    other = {"ticker": "NVDA", "action": "BUY", "reference_price": 100.0, "core": True}  # flag alone is not enough
    assert g.stale_recommendation_reason(core_rec) is None
    assert "flipped" in g.stale_recommendation_reason(other)


# --- core top-up --------------------------------------------------------------------


def test_gap_and_order_size():
    assert ch.core_gap_pct(100_000, 30_000, CORE) == pytest.approx(40.0)
    assert ch.core_gap_pct(100_000, 72_000, CORE) == pytest.approx(-2.0)
    # gap 40%, capped at 10% of 100k = 10,000 -> 12 shares at $778
    assert ch.core_order_qty(100_000, 80_000, 40.0, 778.0, CORE) == 12
    # limited by cash: only 3,000 on hand
    assert ch.core_order_qty(100_000, 3_000, 40.0, 778.0, CORE) == 3
    # a small gap is the whole gap: 6% of 100k = 6,000 -> 7 shares
    assert ch.core_order_qty(100_000, 80_000, 6.0, 778.0, CORE) == 7
    assert ch.core_order_qty(100_000, 80_000, 6.0, 0.0, CORE) == 0


@pytest.fixture
def core_env(monkeypatch, tmp_path):
    placed, decisions, logged = [], [], []
    monkeypatch.setattr(ch, "daily_loss_reason", lambda: None)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", lambda: {"equity": "73500", "cash": "67000"})
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_positions", lambda: [{"symbol": "TSLA", "market_value": "4500"}])
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote",
                        lambda s: {"ask_price": "778.2", "bid_price": "778.0"})
    monkeypatch.setattr(ch, "record_decision", lambda *a, **k: decisions.append((a, k)))
    monkeypatch.setattr(ch, "submit_approved_order", lambda rec, qty, source: placed.append((rec, qty, source)) or {"id": "o1"})
    monkeypatch.setattr(ch, "_persist_attempt", lambda result, cp: logged.append((result, cp)))
    return placed, decisions, logged


def test_core_tops_up_at_its_checkpoint_as_an_ordinary_approved_order(core_env):
    placed, decisions, logged = core_env
    out = ch.core_rebalance("midday")
    assert [r["status"] for r in out] == ["submitted"]
    rec, qty, source = placed[0]
    assert (rec["ticker"], rec["action"], rec["checkpoint"], rec["core"], source) == ("SPY", "BUY", "core", True, "core")
    assert qty == 9  # 10% of 73,500 = 7,350 / 778.2 -> 9 whole shares
    assert decisions[0][0] == ("SPY", "core", "approve", {"qty": 9, "source": "core"})
    assert logged[0][0]["status"] == "submitted"


def test_core_does_nothing_off_checkpoint_when_off_when_in_band_or_when_halted(core_env, monkeypatch):
    placed, _, _ = core_env
    assert ch.core_rebalance("pre_close") == []
    monkeypatch.setattr(ch, "daily_loss_reason", lambda: "Daily loss -3% has reached the cap")
    assert ch.core_rebalance("midday") == []
    monkeypatch.setattr(ch, "daily_loss_reason", lambda: None)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_positions",
                        lambda: [{"symbol": "SPY", "market_value": "50000"}])  # 68% of 73,500: within 5 of 70
    assert ch.core_rebalance("midday") == []
    monkeypatch.setattr(ch, "core_cfg", lambda: {})
    assert ch.core_rebalance("midday") == []
    assert placed == []


def test_a_refusal_or_error_is_that_attempts_own_result_and_never_raises(core_env, monkeypatch):
    _, _, logged = core_env

    def refuse(rec, qty, source):
        raise ch.OrderRefused("SPY has a one-sided quote")

    monkeypatch.setattr(ch, "submit_approved_order", refuse)
    assert ch.core_rebalance("midday")[0]["status"] == "refused"

    def boom(rec, qty, source):
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(ch, "submit_approved_order", boom)
    out = ch.core_rebalance("midday")
    assert out[0]["status"] == "error" and "alpaca down" in out[0]["reason"]
    assert [r[0]["status"] for r in logged] == ["refused", "error"]


# --- the satellite pass leaves the core alone -----------------------------------------


def test_the_core_symbol_is_never_researched_or_scored_by_the_satellite_pass(monkeypatch):
    monkeypatch.setattr(orch, "load_watchlist", lambda: ["SPY", "GOOG"])
    monkeypatch.setattr(orch, "get_market_movers", lambda: {"gainers": []})
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "SPY", "unrealized_plpc": 0.01}])
    monkeypatch.setattr(orch, "daily_loss_reason", lambda: None)
    monkeypatch.setattr(orch, "core_symbol", lambda: "SPY")
    monkeypatch.setattr(orch, "get_market_return_pct", lambda s: None)
    monkeypatch.setattr(orch, "get_fundamentals", lambda t: None)
    monkeypatch.setattr(orch, "get_recent_bars", lambda t, **k: [])
    monkeypatch.setattr(orch, "get_mid_price", lambda t: None)
    monkeypatch.setattr(orch, "technical_score", lambda b: 0.5)
    monkeypatch.setattr(orch, "save_recommendation", lambda r: None)
    monkeypatch.setattr(orch, "auto_apply", lambda recs, cp: [])
    monkeypatch.setattr(orch, "assess_entry", lambda *a, **k: None)
    monkeypatch.setattr(orch, "buy_checkpoint_reason", lambda rec: None)
    researched = []
    monkeypatch.setattr(orch, "research_ticker",
                        lambda t, c: researched.append(t) or {"headline_summary": "x", "sources": ["s"]})
    monkeypatch.setattr(
        orch, "score_candidate",
        lambda **kw: {"ticker": kw["ticker"], "checkpoint": kw["checkpoint"], "action": "HOLD", "confidence": 50},
    )
    seen = {}
    monkeypatch.setattr(orch, "notify_digest", lambda recs, cp, auto_results=None, **k: seen.update(auto=auto_results))
    monkeypatch.setattr(orch, "core_rebalance", lambda cp: [{"ticker": "SPY", "status": "submitted", "qty": 9}])

    orch.run_checkpoint("midday")

    assert researched == ["GOOG"]
    assert seen["auto"] == [{"ticker": "SPY", "status": "submitted", "qty": 9}]


def test_a_core_failure_never_breaks_the_checkpoint(monkeypatch):
    monkeypatch.setattr(orch, "load_watchlist", lambda: [])
    monkeypatch.setattr(orch, "get_market_movers", lambda: {"gainers": []})
    monkeypatch.setattr(orch, "get_positions", lambda: [])
    monkeypatch.setattr(orch, "daily_loss_reason", lambda: None)
    monkeypatch.setattr(orch, "core_symbol", lambda: None)
    monkeypatch.setattr(orch, "get_market_return_pct", lambda s: None)
    monkeypatch.setattr(orch, "notify_digest", lambda *a, **k: None)

    def boom(cp):
        raise RuntimeError("core exploded")

    monkeypatch.setattr(orch, "core_rebalance", boom)
    assert orch.run_checkpoint("midday") == []


def _score_one(monkeypatch, checkpoint, action):
    monkeypatch.setattr(orch, "load_watchlist", lambda: ["GOOG"])
    monkeypatch.setattr(orch, "get_market_movers", lambda: {"gainers": []})
    monkeypatch.setattr(orch, "get_positions", lambda: [])
    monkeypatch.setattr(orch, "daily_loss_reason", lambda: None)
    monkeypatch.setattr(orch, "core_symbol", lambda: None)
    monkeypatch.setattr(orch, "core_rebalance", lambda cp: [])
    monkeypatch.setattr(orch, "get_market_return_pct", lambda s: None)
    monkeypatch.setattr(orch, "get_fundamentals", lambda t: None)
    monkeypatch.setattr(orch, "get_recent_bars", lambda t, **k: [])
    monkeypatch.setattr(orch, "get_mid_price", lambda t: None)
    monkeypatch.setattr(orch, "technical_score", lambda b: 0.5)
    monkeypatch.setattr(orch, "save_recommendation", lambda r: None)
    monkeypatch.setattr(orch, "auto_apply", lambda recs, cp: [])
    monkeypatch.setattr(orch, "assess_entry", lambda *a, **k: None)
    monkeypatch.setattr(orch, "notify_digest", lambda *a, **k: None)
    monkeypatch.setattr(orch, "research_ticker", lambda t, c: {"headline_summary": "x", "sources": ["s"]})
    monkeypatch.setattr(
        orch, "score_candidate",
        lambda **kw: {"ticker": kw["ticker"], "checkpoint": kw["checkpoint"], "action": action,
                      "confidence": 90, "suggested_size_pct_of_portfolio": 3.0},
    )
    return orch.run_checkpoint(checkpoint)[0]


def test_a_pre_open_buy_is_reported_hold_with_the_reason_and_a_market_open_buy_is_not(monkeypatch):
    blocked = _score_one(monkeypatch, "pre_open", "BUY")
    assert blocked["action"] == "HOLD" and "pre_open" in blocked["blocked_checkpoint"]
    assert _score_one(monkeypatch, "market_open", "BUY")["action"] == "BUY"
    assert _score_one(monkeypatch, "pre_open", "SELL")["action"] == "SELL"

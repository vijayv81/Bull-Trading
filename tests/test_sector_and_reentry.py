"""Sector cap fixes and the re-entry cooldown (user instruction 2026-10-30).
In the week to 10-29, AAPL, CVX and V were refused 5+ times as "Unclassified
sector over 30%": Yahoo had returned them once without a sector, that was cached
for 30 days, and the SPY core (34% of the account, no sector) filled the bucket.
And AMD was taken profit on at $559.11 on 10-27, then bought back at $562.96 the
same afternoon and at $568.85 on 10-29; it was $527 that evening."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from trading_agent import guardrails as g
from trading_agent import orchestrator as orch
from trading_agent.data import alpaca_client as ac
from trading_agent.data import market_data as md

# --- sector lookups --------------------------------------------------------------------


@pytest.fixture
def ref(monkeypatch, tmp_path):
    path = tmp_path / "yahoo_reference.json"
    monkeypatch.setattr(md, "REFERENCE_CACHE_PATH", path)
    monkeypatch.setattr(md, "_SECTOR_CACHE", {})
    return path


def test_a_missing_sector_on_a_stock_is_not_cached(monkeypatch, ref):
    monkeypatch.setattr(md, "_yahoo_info", lambda t: {"quoteType": "EQUITY", "shortName": "Apple"})
    assert md.get_sector("AAPL") is None
    assert not ref.exists() or "AAPL" not in json.loads(ref.read_text()).get("sector", {})
    # next lookup (a fresh process) asks Yahoo again and gets the real answer
    monkeypatch.setattr(md, "_SECTOR_CACHE", {})
    monkeypatch.setattr(md, "_yahoo_info", lambda t: {"quoteType": "EQUITY", "sector": "Technology"})
    assert md.get_sector("AAPL") == "Technology"
    assert json.loads(ref.read_text())["sector"]["AAPL"]["value"] == "Technology"


def test_a_fund_with_no_sector_is_a_real_answer_and_is_cached(monkeypatch, ref):
    monkeypatch.setattr(md, "_yahoo_info", lambda t: {"quoteType": "ETF"})
    assert md.get_sector("SPY") is None
    assert json.loads(ref.read_text())["sector"]["SPY"]["value"] is None


def test_a_stale_real_sector_is_kept_when_yahoo_returns_a_partial_answer(monkeypatch, ref):
    at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    ref.write_text(json.dumps({"sector": {"CVX": {"value": "Energy", "at": at}}}))
    monkeypatch.setattr(md, "_yahoo_info", lambda t: {"quoteType": "EQUITY"})
    assert md.get_sector("CVX") == "Energy"


# --- the core is left out of sector totals ------------------------------------------------


@pytest.fixture
def book(monkeypatch):
    positions = [
        {"symbol": "SPY", "market_value": "25200"},  # the core, no sector
        {"symbol": "XOM", "market_value": "2600"},
    ]
    monkeypatch.setattr(g, "_account", lambda: {"equity": "74000"})
    monkeypatch.setattr(g, "_positions", lambda: positions)
    monkeypatch.setattr(g, "_reference_price", lambda s: 200.0)
    monkeypatch.setattr(g, "_sector_bucket", lambda s: {"XOM": "Energy"}.get(s, g.UNCLASSIFIED_SECTOR))
    return positions


def _limits(core_enabled=True):
    return lambda: {
        "portfolio": {"max_sector_concentration_pct": 30.0},
        "core": {"enabled": core_enabled, "symbol": "SPY", "max_position_pct_of_portfolio": 40},
    }


def test_an_unclassified_name_is_not_refused_because_of_the_core(monkeypatch, book):
    monkeypatch.setattr(g, "load_risk_limits", _limits(core_enabled=True))
    assert g.sector_concentration_reason("CVX", "BUY", 10) is None  # 2,000 of 74,000 = 2.7%


def test_without_a_core_the_same_holding_still_counts(monkeypatch, book):
    monkeypatch.setattr(g, "load_risk_limits", _limits(core_enabled=False))
    assert "Unclassified" in g.sector_concentration_reason("CVX", "BUY", 10)


# --- re-entry cooldown ---------------------------------------------------------------------


AMD_SELL = {"symbol": "AMD", "side": "sell", "filled_avg_price": "559.11", "filled_at": "2026-10-27T16:36:00Z"}
AMD_BUY = {"symbol": "AMD", "side": "buy", "filled_avg_price": "515.22", "filled_at": "2026-10-23T13:43:00Z"}


@pytest.fixture
def cooldown(monkeypatch):
    state = {"fills": [AMD_BUY, AMD_SELL], "cfg": {"reentry_cooldown_days": 3, "reentry_below_exit_allowed": True}}
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"execution": state["cfg"]})
    monkeypatch.setattr(ac, "get_filled_orders", lambda since=None: state["fills"])
    return state


def test_the_amd_rebuys_are_refused(cooldown):
    same_day = g.reentry_cooldown_reason("AMD", "BUY", 562.96, today=date(2026, 10, 27))
    assert "sold on 2026-10-27 at $559.11, 0 trading day(s) ago" in same_day
    assert g.reentry_cooldown_reason("AMD", "BUY", 568.85, today=date(2026, 10, 29)) is not None


def test_a_rebuy_below_the_exit_price_is_allowed(cooldown):
    assert g.reentry_cooldown_reason("AMD", "BUY", 550.00, today=date(2026, 10, 28)) is None


def test_the_cooldown_ends_after_three_trading_days(cooldown):
    assert g.reentry_cooldown_reason("AMD", "BUY", 600.0, today=date(2026, 10, 30)) is not None  # Fri: 3rd day
    assert g.reentry_cooldown_reason("AMD", "BUY", 600.0, today=date(2026, 11, 2)) is None  # Mon: over


def test_no_recent_exit_sells_and_zero_days_are_never_refused(cooldown):
    assert g.reentry_cooldown_reason("MSFT", "BUY", 528.0, today=date(2026, 10, 28)) is None
    assert g.reentry_cooldown_reason("AMD", "SELL", 600.0, today=date(2026, 10, 28)) is None
    cooldown["cfg"]["reentry_cooldown_days"] = 0
    assert g.reentry_cooldown_reason("AMD", "BUY", 600.0, today=date(2026, 10, 28)) is None


def test_below_exit_exception_can_be_turned_off(cooldown):
    cooldown["cfg"]["reentry_below_exit_allowed"] = False
    assert g.reentry_cooldown_reason("AMD", "BUY", 500.0, today=date(2026, 10, 28)) is not None


def test_unreadable_fills_fail_closed(monkeypatch, cooldown):
    def boom(since=None):
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(ac, "get_filled_orders", boom)
    assert "refusing to proceed blind" in g.reentry_cooldown_reason("AMD", "BUY", 560.0, today=date(2026, 10, 28))


def test_a_buy_in_cooldown_is_reported_hold_at_scoring(monkeypatch):
    for name, value in {
        "daily_loss_reason": lambda: None,
        "get_market_movers": lambda: {"gainers": []},
        "get_positions": lambda: [],
        "get_fundamentals": lambda t: None,
        "get_recent_bars": lambda t, **k: [],
        "get_live_price": lambda t: (562.96, "live_trade"),
        "get_market_return_pct": lambda s: None,
        "technical_score": lambda b: 0.5,
        "save_recommendation": lambda r: None,
        "assess_entry": lambda *a, **k: None,
        "buy_checkpoint_reason": lambda rec: None,
        "core_rebalance": lambda cp: [],
        "core_symbol": lambda: None,
        "sourcing_cfg": lambda cfg: {"mode": "movers"},
        "notify_digest": lambda *a, **k: None,
        "notify_research_degraded": lambda *a: None,
        "auto_apply": lambda recs, cp: [],
        "load_watchlist": lambda: ["AMD"],
        "research_ticker": lambda t, c: {"headline_summary": "x", "sources": ["s"]},
        "score_candidate": lambda **kw: {"ticker": kw["ticker"], "checkpoint": kw["checkpoint"],
                                         "action": "BUY", "confidence": 93, "suggested_size_pct_of_portfolio": 3},
        "reentry_cooldown_reason": lambda t, side, price: "AMD was sold on 2026-10-27 at $559.11",
    }.items():
        monkeypatch.setattr(orch, name, value)
    rec = orch.run_checkpoint("market_open")[0]
    assert rec["action"] == "HOLD" and "559.11" in rec["blocked_reentry"]


def test_the_order_gate_refuses_a_buy_in_cooldown(monkeypatch):
    from trading_agent.execute import order_manager as om

    for name, value in {
        "load_risk_limits": lambda: {"operational": {"trading_enabled": True}},
        "min_confidence_reason": lambda *a: None,
        "entry_quality_reason": lambda rec: None,
        "buy_checkpoint_reason": lambda rec: None,
        "daily_trade_count_reason": lambda side=None: None,
        "get_decision": lambda t, c: {"decision": "approve", "timestamp": "x", "terms": {"qty": 3}},
        "is_expired": lambda ts: False,
        "stale_recommendation_reason": lambda rec: None,
        "daily_loss_reason": lambda: None,
        "halt_allows_exits": lambda: True,
        "entry_limit_price": lambda t, s: (None, 574.54),
        "instrument_reason": lambda t, s, p=None: None,
        "reentry_cooldown_reason": lambda t, s: "AMD was sold on 2026-10-27 at $559.11",
    }.items():
        monkeypatch.setattr(om, name, value)
    rec = {"ticker": "AMD", "checkpoint": "market_open", "action": "BUY", "confidence": 93}
    with pytest.raises(om.OrderRefused, match="559.11"):
        om.submit_approved_order(rec, 3)

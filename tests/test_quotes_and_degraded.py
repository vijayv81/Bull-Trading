"""The spread check uses the real market's spread, and degraded research is
said out loud (user instruction 2026-10-22: the only BUY signals that week, AMD
and TXN, were refused on IEX's 8-12% book while the consolidated spread was
0.03-1%; and a whole day ran on headline fallbacks with Perplexity returning 401
and no notice)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from trading_agent import guardrails as g
from trading_agent import orchestrator as orch
from trading_agent.data import alpaca_client as ac
from trading_agent.notify import approval_gateway as gw
from trading_agent.research import perplexity_client as pc

EXEC = {"order_type": "limit", "limit_slippage_pct": 1.0, "max_spread_pct": 5.0, "spread_quote_feed": "delayed_sip"}


@pytest.fixture
def market(monkeypatch):
    state = {
        "iex": {"bid_price": 521.0, "ask_price": 549.86},
        "sip": {"bid_price": 534.6, "ask_price": 534.8},
        "live": (534.7, "live_trade"),
        "exec": dict(EXEC),
    }
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"execution": state["exec"]})
    monkeypatch.setattr(ac, "get_latest_quote", lambda s: state["iex"])

    def delayed(s):
        if isinstance(state["sip"], Exception):
            raise state["sip"]
        return state["sip"]

    monkeypatch.setattr(ac, "get_delayed_quote", delayed)
    monkeypatch.setattr(ac, "get_live_price", lambda s, **k: state["live"])
    return state


# --- spread check and limit -----------------------------------------------------------


def test_a_liquid_name_with_a_wide_iex_book_is_not_refused(market):
    reason, limit = g.entry_limit_price("AMD", "BUY")
    assert reason is None
    assert limit == pytest.approx(540.05)  # the fresh last trade + 1%, not the IEX ask


def test_a_genuinely_thin_name_is_still_refused_on_the_consolidated_quote(market):
    market["sip"] = {"bid_price": 1.96, "ask_price": 2.31}
    reason, limit = g.entry_limit_price("MGLD", "BUY")
    assert limit is None and "16.4%" in reason and "consolidated" in reason


def test_without_a_delayed_quote_it_falls_back_to_the_iex_spread(market):
    market["sip"] = RuntimeError("feed down")
    reason, _ = g.entry_limit_price("AMD", "BUY")
    assert "5.4%" in reason and "IEX quote" in reason


def test_spread_quote_feed_iex_is_the_revert(market):
    market["exec"]["spread_quote_feed"] = "iex"
    reason, _ = g.entry_limit_price("AMD", "BUY")
    assert reason is not None and "IEX" in reason


def test_a_one_sided_consolidated_quote_still_refuses(market):
    market["sip"] = {"bid_price": 0.0, "ask_price": 16.0}
    market["iex"] = {"bid_price": 0.0, "ask_price": 16.0}
    reason, _ = g.entry_limit_price("FVNNU", "BUY")
    assert "one-sided" in reason


def test_limit_base_falls_back_to_iex_ask_then_delayed_ask(market):
    market["live"] = (534.0, "live_mid")  # no fresh trade
    assert g.entry_limit_price("AMD", "BUY")[1] == pytest.approx(round(549.86 * 1.01, 2))
    market["iex"] = {"bid_price": 165.0, "ask_price": 0.0}
    market["sip"] = {"bid_price": 165.9, "ask_price": 166.0}
    assert g.entry_limit_price("PG", "BUY")[1] == pytest.approx(round(166.0 * 1.01, 2))


def test_sells_are_unaffected(market):
    assert g.entry_limit_price("AMD", "SELL") == (None, None)


# --- live price -----------------------------------------------------------------------------


def _trade(price, minutes_ago):
    return {"price": price, "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()}


def test_live_price_prefers_a_fresh_trade_then_the_quote_mid(monkeypatch):
    monkeypatch.setattr(ac, "get_mid_price", lambda t: 507.99)
    monkeypatch.setattr(ac, "get_latest_trade", lambda t: _trade(508.14, 2))
    assert ac.get_live_price("AMD") == (508.14, "live_trade")
    monkeypatch.setattr(ac, "get_latest_trade", lambda t: _trade(508.14, 120))
    assert ac.get_live_price("AMD") == (507.99, "live_mid")

    def boom(t):
        raise RuntimeError("no trades")

    monkeypatch.setattr(ac, "get_latest_trade", boom)
    assert ac.get_live_price("AMD") == (507.99, "live_mid")
    monkeypatch.setattr(ac, "get_mid_price", lambda t: None)
    assert ac.get_live_price("AMD") == (None, None)


def test_the_stale_check_price_is_the_live_price(monkeypatch):
    monkeypatch.setattr(ac, "get_live_price", lambda t, **k: (101.0, "live_trade"))
    assert g._mid_price("AMD") == 101.0
    monkeypatch.setattr(ac, "get_live_price", lambda t, **k: (None, None))
    with pytest.raises(ValueError):
        g._mid_price("AMD")


# --- degraded research ------------------------------------------------------------------------


@pytest.fixture
def no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "_cached_extraction", lambda t, c: None)
    monkeypatch.setattr(pc, "_raw_path", lambda t, c: tmp_path / f"{t}_{c}.json")


def test_a_fallback_result_carries_why_perplexity_did_not_answer(monkeypatch, no_cache):
    def unauthorized(prompt):
        raise RuntimeError("401 Client Error: Unauthorized")

    monkeypatch.setattr(pc, "query_perplexity", unauthorized)
    monkeypatch.setattr("trading_agent.data.market_data.fetch_news_headlines",
                        lambda t, limit=5: [{"title": "Headline", "url": "https://x"}])
    out = pc.research_ticker("AMD", "midday")
    assert out["research_source"] == "yahoo_finance_fallback" and "401" in out["primary_error"]


def test_a_perplexity_result_has_no_primary_error(monkeypatch, no_cache):
    monkeypatch.setattr(pc, "query_perplexity", lambda prompt: {"output": []})
    monkeypatch.setattr(pc, "_extract_text", lambda raw: "Real synthesis")
    monkeypatch.setattr(pc, "_extract_sources", lambda raw: ["https://s"])
    out = pc.research_ticker("AMD", "midday")
    assert out["research_source"] == "perplexity" and "primary_error" not in out


@pytest.fixture
def checkpoint(monkeypatch):
    for name, value in {
        "daily_loss_reason": lambda: None,
        "get_market_movers": lambda: {"gainers": []},
        "get_positions": lambda: [],
        "get_fundamentals": lambda t: None,
        "get_recent_bars": lambda t, **k: [],
        "get_live_price": lambda t: (None, None),
        "get_market_return_pct": lambda s: None,
        "technical_score": lambda b: 0.5,
        "save_recommendation": lambda r: None,
        "assess_entry": lambda *a, **k: None,
        "buy_checkpoint_reason": lambda rec: None,
        "core_rebalance": lambda cp: [],
        "core_symbol": lambda: None,
        "sourcing_cfg": lambda cfg: {"mode": "movers"},
        "notify_digest": lambda *a, **k: None,
        "auto_apply": lambda recs, cp: [],
        "load_watchlist": lambda: ["A", "B", "C"],
        "score_candidate": lambda **kw: {"ticker": kw["ticker"], "checkpoint": kw["checkpoint"],
                                         "action": "HOLD", "confidence": 50},
    }.items():
        monkeypatch.setattr(orch, name, value)
    calls = {"degraded": [], "outage": []}
    monkeypatch.setattr(orch, "notify_research_degraded", lambda *a: calls["degraded"].append(a))
    monkeypatch.setattr(orch, "notify_research_outage", lambda *a: calls["outage"].append(a))
    return calls


def _research(perplexity_for=()):
    def research(t, c):
        if t in perplexity_for:
            return {"headline_summary": "x", "sources": ["s"], "research_source": "perplexity"}
        return {"headline_summary": "x", "sources": ["s"], "research_source": "yahoo_finance_fallback",
                "primary_error": "401 Client Error: Unauthorized"}
    return research


def test_degraded_research_is_announced_with_the_perplexity_error(monkeypatch, checkpoint):
    monkeypatch.setattr(orch, "research_ticker", _research())
    results = orch.run_checkpoint("midday")
    assert results[0]["research_primary_error"].startswith("401")
    cp, fallback, total, sources, errors = checkpoint["degraded"][0]
    assert (cp, fallback, total, sources) == ("midday", 3, 3, {"yahoo_finance_fallback": 3})
    assert "401" in errors[0]
    assert checkpoint["outage"] == []


def test_not_degraded_when_perplexity_answers_most(monkeypatch, checkpoint):
    monkeypatch.setattr(orch, "research_ticker", _research(perplexity_for=("A", "B")))
    orch.run_checkpoint("midday")
    assert checkpoint["degraded"] == []


def test_an_outage_is_reported_as_an_outage_not_also_as_degraded(monkeypatch, checkpoint):
    def down(t, c):
        raise RuntimeError("No research data available")

    monkeypatch.setattr(orch, "research_ticker", down)
    orch.run_checkpoint("midday")
    assert len(checkpoint["outage"]) == 1 and checkpoint["degraded"] == []


def test_degraded_email_goes_out_once_a_day_and_every_checkpoint_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "RECOMMENDATIONS_DIR", tmp_path)
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    sent = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: sent.append(a))

    gw.notify_research_degraded("pre_open", 15, 15, {"yahoo_finance_fallback": 15}, ["401 Unauthorized"] * 15)
    gw.notify_research_degraded("market_open", 14, 15, {"yahoo_finance_fallback": 14}, ["401 Unauthorized"])

    assert len(sent) == 1
    subject, body = sent[0][0], sent[0][1]
    assert "Perplexity unavailable" in subject and "PERPLEXITY_API_KEY" in body and "401 Unauthorized" in body
    files = sorted(p.name for p in tmp_path.rglob("research_degraded_*.json"))
    assert files == ["research_degraded_market_open.json", "research_degraded_pre_open.json"]

    from trading_agent.reporting import report_builder as rb

    monkeypatch.setattr(rb, "RECOMMENDATIONS_DIR", tmp_path)
    day = next(tmp_path.iterdir()).name
    rows = rb._day_research_degraded(day)
    assert [r["checkpoint"] for r in rows] == ["pre_open", "market_open"]
    assert rows[0]["primary_errors"] == ["401 Unauthorized"]


def test_the_daily_summary_lists_degraded_checkpoints(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    sent = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: sent.append(a))
    from tests.test_approval_gateway import _summary

    gw.notify_daily_summary(_summary(research_degraded=[
        {"checkpoint": "midday", "fallback": 15, "total": 15, "primary_errors": ["401 Unauthorized"]}
    ]))
    plain, html = sent[0][1], sent[0][2]
    assert "midday: 15 of 15 tickers via fallback — 401 Unauthorized" in plain
    assert "Research quality" in html

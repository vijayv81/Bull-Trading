"""Research outages and candidate sourcing (user instruction 2026-10-21, after
2026-10-16..20: Perplexity returned 401, Yahoo rate-limited this server, nothing
was scored for days and MGLD/TSLA's stop-losses went unchecked; and the week
before, ~200 recommendations from raw movers produced no BUY at all)."""

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trading_agent import orchestrator as orch
from trading_agent.data import market_data as md
from trading_agent.notify import approval_gateway as gw
from trading_agent.scoring import candidates as cand

# --- Yahoo RSS headlines ---------------------------------------------------------

RSS = """<?xml version="1.0"?><rss><channel><title>Yahoo! Finance: TSLA News</title>
<item><title><![CDATA[Tesla &amp; SpaceX explore a merger]]></title><link>https://x/1</link>
<pubDate>Wed, 21 Oct 2026 03:07:11 +0000</pubDate></item>
<item><title>Second headline</title><link>https://x/2</link></item>
<item><title>Third</title><link>https://x/3</link></item>
</channel></rss>"""


def test_rss_items_are_parsed_with_entities_and_cdata():
    items = md._parse_rss_items(RSS, limit=2)
    assert [i["title"] for i in items] == ["Tesla & SpaceX explore a merger", "Second headline"]
    assert items[0]["url"] == "https://x/1" and "2026" in items[0]["published"]


class _Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


def test_fetch_news_headlines_uses_rss_and_never_raises(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, RSS))
    assert len(md.fetch_news_headlines("TSLA")) == 3
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(429))
    assert md.fetch_news_headlines("TSLA") == []

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    assert md.fetch_news_headlines("TSLA") == []


# --- Yahoo reference cache + pacing ------------------------------------------------


@pytest.fixture
def ref(monkeypatch, tmp_path):
    path = tmp_path / "reference" / "yahoo_reference.json"
    monkeypatch.setattr(md, "REFERENCE_CACHE_PATH", path)
    monkeypatch.setattr(md, "_SECTOR_CACHE", {})
    monkeypatch.setattr(md, "_FUNDAMENTALS_CACHE", {})
    return path


def _write(path, kind, ticker, value, days_old):
    path.parent.mkdir(parents=True, exist_ok=True)
    at = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    path.write_text(json.dumps({kind: {ticker: {"value": value, "at": at}}}))


def test_a_fresh_cached_sector_needs_no_yahoo_call(monkeypatch, ref):
    _write(ref, "sector", "TSLA", "Consumer Cyclical", days_old=3)
    monkeypatch.setattr(md, "_yahoo_info", lambda t: pytest.fail("should not call Yahoo"))
    assert md.get_sector("TSLA") == "Consumer Cyclical"


def test_a_stale_sector_is_refreshed_and_stored(monkeypatch, ref):
    _write(ref, "sector", "TSLA", "Old Sector", days_old=40)
    monkeypatch.setattr(md, "_yahoo_info", lambda t: {"sector": "Consumer Cyclical"})
    assert md.get_sector("TSLA") == "Consumer Cyclical"
    assert json.loads(ref.read_text())["sector"]["TSLA"]["value"] == "Consumer Cyclical"


def test_a_failed_lookup_falls_back_to_the_stale_value_then_to_none(monkeypatch, ref):
    def rate_limited(t):
        raise RuntimeError("Too Many Requests. Rate limited.")

    monkeypatch.setattr(md, "_yahoo_info", rate_limited)
    _write(ref, "fundamentals", "TSLA", {"marketCap": 1}, days_old=30)
    assert md.get_fundamentals("TSLA") == {"marketCap": 1}
    assert md.get_fundamentals("NEWCO") is None
    assert md.get_sector("NEWCO") is None


def test_yahoo_info_waits_and_retries_once_on_a_rate_limit(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(md.time, "sleep", lambda s: sleeps.append(s))

    class Ticker:
        def __init__(self, t):
            pass

        @property
        def info(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")
            return {"sector": "Technology"}

    monkeypatch.setattr(md.yf, "Ticker", Ticker)
    assert md._yahoo_info("AAPL") == {"sector": "Technology"}
    assert len(calls) == 2 and md.YAHOO_RETRY_BACKOFF_S in sleeps


def test_yahoo_info_does_not_retry_other_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(md.time, "sleep", lambda s: None)

    class Ticker:
        def __init__(self, t):
            pass

        @property
        def info(self):
            calls.append(1)
            raise KeyError("bad symbol")

    monkeypatch.setattr(md.yf, "Ticker", Ticker)
    with pytest.raises(KeyError):
        md._yahoo_info("ZZZZ")
    assert len(calls) == 1


# --- held positions are monitored without research; outage alert ----------------------


@pytest.fixture
def checkpoint(monkeypatch):
    monkeypatch.setattr(orch, "daily_loss_reason", lambda: None)
    monkeypatch.setattr(orch, "get_market_movers", lambda: {"gainers": []})
    monkeypatch.setattr(orch, "get_fundamentals", lambda t: None)
    monkeypatch.setattr(orch, "get_recent_bars", lambda t, **k: [])
    monkeypatch.setattr(orch, "get_live_price", lambda t: (None, None))
    monkeypatch.setattr(orch, "get_market_return_pct", lambda s: None)
    monkeypatch.setattr(orch, "technical_score", lambda b: 0.5)
    monkeypatch.setattr(orch, "save_recommendation", lambda r: None)
    monkeypatch.setattr(orch, "assess_entry", lambda *a, **k: None)
    monkeypatch.setattr(orch, "buy_checkpoint_reason", lambda rec: None)
    monkeypatch.setattr(orch, "core_rebalance", lambda cp: [])
    monkeypatch.setattr(orch, "core_symbol", lambda: None)
    monkeypatch.setattr(orch, "sourcing_cfg", lambda cfg: {"mode": "movers"})
    monkeypatch.setattr(orch, "notify_digest", lambda *a, **k: None)
    auto = []
    monkeypatch.setattr(orch, "auto_apply", lambda recs, cp: auto.append(recs) or [])
    alerts = []
    monkeypatch.setattr(orch, "notify_research_outage", lambda *a: alerts.append(a))
    return auto, alerts


def _research_down(ticker, checkpoint):
    raise RuntimeError("401 Client Error: Unauthorized")


def _score(action):
    return lambda **kw: {"ticker": kw["ticker"], "checkpoint": kw["checkpoint"], "action": action,
                         "confidence": 70, "suggested_size_pct_of_portfolio": 3.0,
                         "position_pnl_pct": kw.get("position_pnl_pct"), "stop_loss_pct": 4.0}


def test_a_held_position_is_scored_without_research_and_can_still_sell(monkeypatch, checkpoint):
    auto, alerts = checkpoint
    monkeypatch.setattr(orch, "load_watchlist", lambda: ["NEWCO"])
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "MGLD", "unrealized_plpc": -0.12}])
    monkeypatch.setattr(orch, "research_ticker", _research_down)
    monkeypatch.setattr(orch, "score_candidate", _score("SELL"))

    results = orch.run_checkpoint("midday")

    assert [r["ticker"] for r in results] == ["MGLD"]  # NEWCO is skipped, as before
    rec = results[0]
    assert rec["action"] == "SELL" and "401" in rec["research_unavailable"]
    assert rec["rationale"].startswith("Research unavailable")  # not blank, so the digest shows it
    assert auto[0][0]["ticker"] == "MGLD"  # the exit reaches auto-apply


def test_a_held_buy_signal_without_research_becomes_hold(monkeypatch, checkpoint):
    monkeypatch.setattr(orch, "load_watchlist", lambda: [])
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "TSLA", "unrealized_plpc": 0.02}])
    monkeypatch.setattr(orch, "research_ticker", _research_down)
    monkeypatch.setattr(orch, "score_candidate", _score("BUY"))
    rec = orch.run_checkpoint("midday")[0]
    assert rec["action"] == "HOLD" and "never made without" in rec["blocked_research"]


def test_the_switch_off_restores_no_research_no_score(monkeypatch, checkpoint):
    monkeypatch.setattr(orch, "load_watchlist", lambda: [])
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "TSLA", "unrealized_plpc": -0.1}])
    monkeypatch.setattr(orch, "research_ticker", _research_down)
    monkeypatch.setattr(orch, "score_candidate", _score("SELL"))
    real = orch.load_risk_limits

    def limits():
        cfg = real()
        return {**cfg, "position": {**cfg.get("position", {}), "monitor_held_without_research": False}}

    monkeypatch.setattr(orch, "load_risk_limits", limits)
    assert orch.run_checkpoint("midday") == []


def test_an_outage_is_announced_when_most_tickers_fail(monkeypatch, checkpoint):
    _, alerts = checkpoint
    monkeypatch.setattr(orch, "load_watchlist", lambda: ["A", "B", "C"])
    monkeypatch.setattr(orch, "get_positions", lambda: [])
    monkeypatch.setattr(orch, "research_ticker",
                        lambda t, c: _research_down(t, c) if t != "C" else {"headline_summary": "ok", "sources": ["s"]})
    monkeypatch.setattr(orch, "score_candidate", _score("HOLD"))
    orch.run_checkpoint("market_open")
    assert len(alerts) == 1
    cp, failed, attempted, errors = alerts[0]
    assert (cp, failed, attempted) == ("market_open", 2, 3) and "401" in errors[0]


def test_no_outage_alert_when_most_tickers_succeed(monkeypatch, checkpoint):
    _, alerts = checkpoint
    monkeypatch.setattr(orch, "load_watchlist", lambda: ["A", "B", "C"])
    monkeypatch.setattr(orch, "get_positions", lambda: [])
    monkeypatch.setattr(orch, "research_ticker",
                        lambda t, c: _research_down(t, c) if t == "A" else {"headline_summary": "ok", "sources": ["s"]})
    monkeypatch.setattr(orch, "score_candidate", _score("HOLD"))
    orch.run_checkpoint("market_open")
    assert alerts == []


def test_the_outage_is_recorded_emailed_and_shown_in_the_daily_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "RECOMMENDATIONS_DIR", tmp_path)
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    sent = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: sent.append(a))
    gw.notify_research_outage("midday", 17, 17, ["401 Unauthorized"] * 5)

    record = json.loads(next(tmp_path.rglob("research_outage_midday.json")).read_text())[0]
    assert (record["failed"], record["attempted"], record["errors"]) == (17, 17, ["401 Unauthorized"])
    subject, body = sent[0][0], sent[0][1]
    assert "research failed for 17 of 17" in subject and "PERPLEXITY_API_KEY" in body

    from trading_agent.reporting import report_builder as rb

    monkeypatch.setattr(rb, "RECOMMENDATIONS_DIR", tmp_path)
    day = next(tmp_path.iterdir()).name
    assert rb._day_research_outages(day)[0]["failed"] == 17


# --- liquid candidate sourcing ----------------------------------------------------------


CFG = {**cand.DEFAULTS, "universe": ["AAPL", "MSFT", "PENNY", "WILD", "THIN"], "max_candidates": 2}


def _bars(n, close, volume, range_pct, trend=0.0):
    rows = []
    for i in range(n):
        c = close * (1 + trend * (i - n) / n)
        half = c * range_pct / 200
        rows.append({"open": c, "high": c + half, "low": c - half, "close": c, "volume": volume})
    return rows


def test_screen_keeps_only_liquid_calm_names_and_ranks_by_technical(monkeypatch):
    import trading_agent.scoring.recommendation_engine as re_

    monkeypatch.setattr(re_, "technical_score", lambda df: float(df["close"].iloc[-1]) / 1000)
    rows = cand.screen({
        "AAPL": _bars(80, 300, 40_000_000, 1.8),
        "MSFT": _bars(80, 500, 20_000_000, 1.5),
        "PENNY": _bars(80, 1.9, 900_000_000, 3.0),  # under $10
        "WILD": _bars(80, 40, 50_000_000, 12.0),  # 12% a day
        "THIN": _bars(80, 50, 100_000, 2.0),  # $5M a day
        "NEW": _bars(30, 80, 10_000_000, 2.0),  # too little history
    }, CFG)
    assert [r["symbol"] for r in rows] == ["MSFT", "AAPL"]


def test_select_excludes_watchlist_held_and_core_includes_movers_and_caps(monkeypatch):
    seen = {}

    def bars(symbols, lookback_days):
        seen["pool"] = set(symbols)
        return {s: [] for s in symbols}

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars_multi", bars)
    monkeypatch.setattr(cand, "screen", lambda b, cfg: [{"symbol": s} for s in ["X1", "X2", "X3"]])
    picks = cand.select_liquid_candidates({"gainers": [{"symbol": "BRZE"}], "losers": []}, {"AAPL", "SPY"}, CFG)
    assert picks == ["X1", "X2"]  # max_candidates
    assert "BRZE" in seen["pool"] and "AAPL" not in seen["pool"]
    monkeypatch.setattr(cand, "screen", lambda b, cfg: [])
    cand.select_liquid_candidates({"gainers": [{"symbol": "BRZE"}]}, set(), {**CFG, "include_movers": False})
    assert "BRZE" not in seen["pool"]


def test_a_bars_failure_returns_no_candidates_instead_of_raising(monkeypatch):
    def boom(symbols, lookback_days):
        raise RuntimeError("alpaca down")

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars_multi", boom)
    assert cand.select_liquid_candidates({}, set(), CFG) == []


def test_liquid_mode_adds_the_screened_names_and_skips_raw_movers(monkeypatch, checkpoint):
    monkeypatch.setattr(orch, "sourcing_cfg", lambda cfg: {"mode": "liquid"})
    monkeypatch.setattr(orch, "load_watchlist", lambda: ["TSLA"])
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "MGLD", "unrealized_plpc": 0.01}])
    monkeypatch.setattr(orch, "core_symbol", lambda: "SPY")
    monkeypatch.setattr(orch, "get_market_movers", lambda: {"gainers": [{"symbol": "JUNK", "price": 1.2}], "losers": []})
    excluded = {}

    def select(movers, exclude, cfg):
        excluded.update(exclude=exclude)
        return ["AMD", "META"]

    monkeypatch.setattr(orch, "select_liquid_candidates", select)
    researched = []
    monkeypatch.setattr(orch, "research_ticker",
                        lambda t, c: researched.append(t) or {"headline_summary": "ok", "sources": ["s"]})
    monkeypatch.setattr(orch, "score_candidate", _score("HOLD"))
    orch.run_checkpoint("market_open")
    assert set(researched) == {"MGLD", "TSLA", "AMD", "META"}  # no JUNK
    assert {"TSLA", "MGLD", "SPY"} <= excluded["exclude"]

"""Safety-critical: auto_apply must never bypass a guardrail, must respect its
own on/off switch and daily cap, and one candidate's failure must not stop
the rest.
"""

import json

import pytest

from trading_agent.execute import auto_pilot as ap


def _rec(ticker, action="BUY", confidence=70, suggested_pct=5.0, checkpoint="pre_open"):
    return {
        "ticker": ticker,
        "checkpoint": checkpoint,
        "action": action,
        "confidence": confidence,
        "suggested_size_pct_of_portfolio": suggested_pct,
    }


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "TRADES_DIR", tmp_path / "trades")
    # Real journal.record_entry() hits live Alpaca (_reference_price) and
    # writes to the real data/journal/ — tests that care about journaling
    # override this again themselves.
    monkeypatch.setattr("trading_agent.journal.record_entry", lambda *a, **k: None)
    # _symbols_at_position_cap() reads live positions; tests that care set them.
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_positions", lambda: [])


def _enabled(max_trades_per_day=5):
    return lambda: {"operational": {"auto_apply": {"enabled": True, "max_trades_per_day": max_trades_per_day}}}


# --- qty_for_recommendation ---------------------------------------------------


def test_qty_for_recommendation_computes_whole_shares():
    rec = _rec("TSLA", suggested_pct=5.0)
    # 5% of 100_000 = 5000; at $110/share -> 45 whole shares (rounds down)
    assert ap.qty_for_recommendation(rec, equity=100_000.0, price=110.0) == 45.0


def test_qty_for_recommendation_zero_when_no_suggested_size():
    rec = _rec("TSLA", suggested_pct=0.0)
    assert ap.qty_for_recommendation(rec, equity=100_000.0, price=100.0) == 0.0


def test_qty_for_recommendation_zero_when_price_unavailable():
    rec = _rec("TSLA", suggested_pct=5.0)
    assert ap.qty_for_recommendation(rec, equity=100_000.0, price=0.0) == 0.0


def _held(monkeypatch, ticker, qty):
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_positions",
        lambda: [{"symbol": ticker, "qty": qty}],
    )


def test_qty_for_recommendation_sell_capped_at_held_qty(monkeypatch):
    # suggested_size_pct_of_portfolio sizes a SELL the same way it would a
    # fresh BUY — 5% of 100_000 at $110/share = 45 shares — with no idea how
    # much is actually held. A stop-loss/take-profit SELL on a small
    # existing position must be capped at what's held, not the fresh-position
    # target, or short_sale_reason() refuses it as an attempted short.
    _held(monkeypatch, "GRMLW", qty=10)
    rec = _rec("GRMLW", action="SELL", suggested_pct=5.0)
    assert ap.qty_for_recommendation(rec, equity=100_000.0, price=110.0) == 10.0


def test_qty_for_recommendation_sell_not_inflated_past_suggested(monkeypatch):
    # Held far exceeds the suggested size — the cap is a ceiling, never a floor.
    _held(monkeypatch, "GRMLW", qty=10_000)
    rec = _rec("GRMLW", action="SELL", suggested_pct=5.0)
    assert ap.qty_for_recommendation(rec, equity=100_000.0, price=110.0) == 45.0


def test_qty_for_recommendation_sell_zero_when_nothing_held(monkeypatch):
    _held(monkeypatch, "GRMLW", qty=0)
    rec = _rec("GRMLW", action="SELL", suggested_pct=5.0)
    assert ap.qty_for_recommendation(rec, equity=100_000.0, price=110.0) == 0.0


def test_qty_for_recommendation_buy_ignores_held_qty(monkeypatch):
    # A BUY is sized purely off suggested_size_pct_of_portfolio — held qty is
    # irrelevant to opening/adding to a position, only to closing one.
    _held(monkeypatch, "TSLA", qty=1)
    rec = _rec("TSLA", action="BUY", suggested_pct=5.0)
    assert ap.qty_for_recommendation(rec, equity=100_000.0, price=110.0) == 45.0


# --- auto_apply: on/off + cap --------------------------------------------------


def test_disabled_by_default_does_nothing(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", lambda: {"operational": {}})
    assert ap.auto_apply([_rec("TSLA")], "pre_open") == []


def test_disabled_explicitly_does_nothing(monkeypatch):
    monkeypatch.setattr(
        ap, "load_risk_limits", lambda: {"operational": {"auto_apply": {"enabled": False}}}
    )
    assert ap.auto_apply([_rec("TSLA")], "pre_open") == []


def test_only_actionable_recs_are_candidates(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    recs = [_rec("TSLA", action="HOLD"), _rec("GOOG", action="BUY")]
    results = ap.auto_apply(recs, "pre_open")
    assert [r["ticker"] for r in results] == ["GOOG"]


def test_picks_highest_confidence_first_up_to_cap(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled(max_trades_per_day=2))
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    recs = [
        _rec("LOW", confidence=55),
        _rec("HIGH", confidence=95),
        _rec("MID", confidence=75),
    ]
    results = ap.auto_apply(recs, "pre_open")
    assert [r["ticker"] for r in results] == ["HIGH", "MID"]


def test_stops_at_remaining_daily_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled(max_trades_per_day=3))
    trades_dir = tmp_path / "trades" / ap.today()
    trades_dir.mkdir(parents=True)
    (trades_dir / "orders_submitted.json").write_text(
        '[{"order": {"symbol": "A"}, "source": "auto"}, {"order": {"symbol": "B"}, "source": "auto"}]'
    )
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    recs = [_rec("ONE", confidence=90), _rec("TWO", confidence=80)]
    results = ap.auto_apply(recs, "pre_open")
    assert len(results) == 1  # only 1 slot left (3 cap - 2 already auto-applied today)


def test_manual_trades_do_not_count_against_the_auto_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled(max_trades_per_day=1))
    trades_dir = tmp_path / "trades" / ap.today()
    trades_dir.mkdir(parents=True)
    (trades_dir / "orders_submitted.json").write_text(
        '[{"order": {"symbol": "A"}, "source": "human"}]'
    )
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    results = ap.auto_apply([_rec("TSLA")], "pre_open")
    assert len(results) == 1
    assert results[0]["status"] == "submitted"


# --- guardrail refusals surface, never bypassed --------------------------------


def test_guardrail_refusal_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)

    def refuse(rec, qty, source):
        raise ap.OrderRefused("over the 5.0% cap")

    monkeypatch.setattr(ap, "submit_approved_order", refuse)
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    results = ap.auto_apply([_rec("TSLA")], "pre_open")
    assert results[0]["status"] == "refused"
    assert "5.0% cap" in results[0]["reason"]


def test_zero_qty_is_skipped_not_submitted(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    submitted = []
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: submitted.append(rec))
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    results = ap.auto_apply([_rec("TSLA", suggested_pct=0.0)], "pre_open")
    assert results[0]["status"] == "skipped"
    assert submitted == []


def test_every_attempt_is_persisted_regardless_of_outcome(monkeypatch, tmp_path):
    """data/trades/ used to only ever record a SUCCESSFUL auto-apply order
    (orders_submitted.json) — a day of nothing-but-refusals was
    indistinguishable from a day nothing was attempted. Every candidate
    auto_apply() considers must now be persisted, refusals/skips/errors
    included, so the daily summary can show what the agent actually did.
    """
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)

    def submit(rec, qty, source):
        if rec["ticker"] == "REFUSED":
            raise ap.OrderRefused("over the 5.0% cap")
        return {"id": "x"}

    monkeypatch.setattr(ap, "submit_approved_order", submit)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"})
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"})

    recs = [
        _rec("SUBMITTED", confidence=95),
        _rec("REFUSED", confidence=90),
        _rec("SKIPPED", confidence=80, suggested_pct=0.0),
    ]
    ap.auto_apply(recs, "pre_open")

    saved = json.loads((tmp_path / "trades" / ap.today() / "auto_apply_attempts_pre_open.json").read_text())
    by_ticker = {a["ticker"]: a for a in saved}
    assert by_ticker["SUBMITTED"]["status"] == "submitted"
    assert by_ticker["REFUSED"]["status"] == "refused"
    assert by_ticker["SKIPPED"]["status"] == "skipped"
    assert all(a["checkpoint"] == "pre_open" for a in saved)


def test_one_candidate_error_does_not_stop_the_rest(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})

    calls = {"n": 0}

    def flaky_account():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("alpaca hiccup")
        return {"equity": "100000"}

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", flaky_account)
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    recs = [_rec("FIRST", confidence=90), _rec("SECOND", confidence=80)]
    results = ap.auto_apply(recs, "pre_open")
    assert results[0]["status"] == "error"
    assert results[1]["status"] == "submitted"


# --- auto-applied trades get journaled ------------------------------------------


def test_submitted_trade_is_journaled(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    calls = []
    monkeypatch.setattr(
        "trading_agent.journal.record_entry",
        lambda ticker, checkpoint, decision, reasoning, action=None, confidence=None: calls.append(
            {"ticker": ticker, "checkpoint": checkpoint, "decision": decision, "action": action}
        ),
    )

    rec = _rec("TSLA", confidence=85)
    rec["component_scores"] = {"technical": 0.8, "sentiment": 0.7, "catalyst": 0.6, "fundamental": None}
    results = ap.auto_apply([rec], "pre_open")

    assert results[0]["status"] == "submitted"
    assert len(calls) == 1
    assert calls[0] == {"ticker": "TSLA", "checkpoint": "pre_open", "decision": "approve", "action": "BUY"}


def test_refused_or_skipped_candidates_are_not_journaled(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)

    def refuse(rec, qty, source):
        raise ap.OrderRefused("over the 5.0% cap")

    monkeypatch.setattr(ap, "submit_approved_order", refuse)
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    calls = []
    monkeypatch.setattr("trading_agent.journal.record_entry", lambda *a, **k: calls.append(1))

    results = ap.auto_apply([_rec("TSLA")], "pre_open")

    assert results[0]["status"] == "refused"
    assert calls == []


def test_journal_failure_does_not_turn_a_submission_into_an_error(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )

    def boom(*a, **k):
        raise RuntimeError("journal write failed")

    monkeypatch.setattr("trading_agent.journal.record_entry", boom)

    results = ap.auto_apply([_rec("TSLA")], "pre_open")

    assert results[0]["status"] == "submitted"


def test_journal_reasoning_includes_sell_pressure_when_present(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"}
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_positions",
        lambda: [{"symbol": "TSLA", "qty": "1000", "market_value": "1000"}],
    )
    reasonings = []
    monkeypatch.setattr(
        "trading_agent.journal.record_entry",
        lambda ticker, checkpoint, decision, reasoning, action=None, confidence=None: reasonings.append(reasoning),
    )

    rec = _rec("TSLA", action="SELL", confidence=85)
    rec["component_scores"] = {"technical": 0.3, "sentiment": None, "catalyst": None, "fundamental": None}
    rec["sell_pressure"] = 0.8
    rec["position_pnl_pct"] = -6.4
    ap.auto_apply([rec], "pre_open")

    assert "sell_pressure=0.8" in reasonings[0]
    assert "position_pnl_pct=-6.4" in reasonings[0]


# --- at-cap BUYs and refusals don't use up slots (2026-10-02 review) ----------


def _market(monkeypatch, positions=()):
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"})
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"})
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_positions", lambda: list(positions))


def _enabled_with_cap(max_trades_per_day=5, cap_pct=5.0):
    return lambda: {
        "operational": {"auto_apply": {"enabled": True, "max_trades_per_day": max_trades_per_day}},
        "position": {"max_position_pct_of_portfolio": cap_pct},
    }


def test_buy_on_a_symbol_already_at_the_cap_is_never_attempted(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled_with_cap())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    submitted = []
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: submitted.append(rec["ticker"]) or {"id": "x"})
    _market(monkeypatch, positions=[{"symbol": "MGLD", "market_value": "5200", "qty": "2600"}])  # 5.2% of 100k

    results = ap.auto_apply([_rec("MGLD", confidence=99), _rec("NEW", confidence=80)], "pre_open")

    assert [r["ticker"] for r in results] == ["NEW"]
    assert submitted == ["NEW"]


def test_sell_of_an_at_cap_position_is_still_attempted(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled_with_cap())
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})
    _market(monkeypatch, positions=[{"symbol": "MGLD", "market_value": "5200", "qty": "2600"}])

    results = ap.auto_apply([_rec("MGLD", action="SELL", confidence=99)], "pre_open")

    assert results[0]["ticker"] == "MGLD" and results[0]["status"] == "submitted"


def test_refusals_do_not_use_up_slots_for_lower_ranked_candidates(monkeypatch):
    """Only the top `remaining` used to be tried, so two refused high-ranked
    picks meant nothing was placed even with good candidates further down."""
    monkeypatch.setattr(ap, "load_risk_limits", _enabled_with_cap(max_trades_per_day=2))
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)

    def submit(rec, qty, source):
        if rec["ticker"].startswith("STALE"):
            raise ap.OrderRefused("price has moved 20.00% ... past the 3.0% drift limit")
        return {"id": "x"}

    monkeypatch.setattr(ap, "submit_approved_order", submit)
    _market(monkeypatch)
    recs = [
        _rec("STALE1", confidence=99),
        _rec("STALE2", confidence=98),
        _rec("GOOD1", confidence=90),
        _rec("GOOD2", confidence=80),
        _rec("GOOD3", confidence=70),
    ]

    results = ap.auto_apply(recs, "pre_open")

    statuses = [(r["ticker"], r["status"]) for r in results]
    assert statuses == [
        ("STALE1", "refused"),
        ("STALE2", "refused"),
        ("GOOD1", "submitted"),
        ("GOOD2", "submitted"),
    ]  # stops once 2 are placed; GOOD3 never tried


# --- candidates the daily cap kept from being tried are logged (2026-10-05) ---


def _logged_attempts(tmp_path, checkpoint):
    from trading_agent.utils import today

    path = tmp_path / "trades" / today() / f"auto_apply_attempts_{checkpoint}.json"
    return json.loads(path.read_text()) if path.exists() else []


def test_when_the_daily_cap_is_already_used_up_every_actionable_candidate_is_logged_capped(monkeypatch, tmp_path):
    from trading_agent.utils import append_json, day_dir

    monkeypatch.setattr(ap, "load_risk_limits", _enabled_with_cap(max_trades_per_day=2))
    append_json(day_dir(ap.TRADES_DIR) / "orders_submitted.json", {"source": "auto", "order": {}})
    append_json(day_dir(ap.TRADES_DIR) / "orders_submitted.json", {"source": "auto", "order": {}})
    submitted = []
    monkeypatch.setattr(ap, "submit_approved_order", lambda *a, **k: submitted.append(1))

    recs = [_rec("IREN", confidence=85), _rec("PDSB", confidence=100), _rec("IREN", confidence=80),
            _rec("HOLDME", action="HOLD", confidence=99)]
    assert ap.auto_apply(recs, "pre_close") == []  # the digest still sees nothing new

    logged = _logged_attempts(tmp_path, "pre_close")
    assert [a["ticker"] for a in logged] == ["PDSB", "IREN"]  # ranked by confidence, each ticker once, no HOLD
    assert {a["status"] for a in logged} == {"capped"}
    assert "2 of 2 orders today" in logged[0]["reason"]
    assert submitted == []


def test_cap_reached_partway_logs_only_the_untried_candidates(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled_with_cap(max_trades_per_day=1))
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: {"id": "x"})
    _market(monkeypatch)

    results = ap.auto_apply([_rec("FIRST", confidence=95), _rec("SECOND", confidence=90), _rec("THIRD", confidence=80)], "midday")

    assert [r["ticker"] for r in results] == ["FIRST"]
    logged = _logged_attempts(tmp_path, "midday")
    assert [(a["ticker"], a["status"]) for a in logged if a["status"] == "capped"] == [
        ("SECOND", "capped"), ("THIRD", "capped")
    ]
    assert "1 of 1 orders today" in logged[-1]["reason"]


def test_timing_wait_is_deferred_logged_and_not_submitted(monkeypatch):
    monkeypatch.setattr(ap, "load_risk_limits", _enabled())
    submitted = []
    monkeypatch.setattr(ap, "record_decision", lambda *a, **k: None)
    monkeypatch.setattr(ap, "submit_approved_order", lambda rec, qty, source: submitted.append(rec["ticker"]) or {})
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", lambda: {"equity": "100000"})
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda t: {"ask_price": "100"})
    waiting = {**_rec("HIGH", confidence=95), "timing": {"verdict": "wait", "reason": "near the month high"}}
    fine = {**_rec("OK", confidence=80), "timing": {"verdict": "proceed", "reason": "ok"}}
    results = ap.auto_apply([waiting, fine], "pre_open")
    assert submitted == ["OK"]
    assert {r["ticker"]: r["status"] for r in results} == {"HIGH": "deferred", "OK": "submitted"}
    persisted = json.loads((ap.TRADES_DIR / ap.today() / "auto_apply_attempts_pre_open.json").read_text())
    assert any(a["ticker"] == "HIGH" and a["status"] == "deferred" for a in persisted)

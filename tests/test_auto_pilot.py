"""Safety-critical: auto_apply must never bypass a guardrail, must respect its
own on/off switch and daily cap, and one candidate's failure must not stop
the rest.
"""

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

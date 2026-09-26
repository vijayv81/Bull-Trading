"""Safety-critical: order_manager must refuse to submit without an approval
record matching, and must always respect the kill switch (plan §8, §9).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from trading_agent.execute import order_manager as om


REC = {"ticker": "TSLA", "checkpoint": "midday", "action": "BUY"}


def _risk(trading_enabled: bool):
    return {"operational": {"trading_enabled": trading_enabled}}


@pytest.fixture(autouse=True)
def guardrails_satisfied(monkeypatch):
    """Account-state guardrails pass by default; the tests that care override them.

    Without this they would reach for live Alpaca account data.
    """
    monkeypatch.setattr(om, "daily_loss_reason", lambda: None)
    monkeypatch.setattr(om, "daily_trade_count_reason", lambda: None)
    monkeypatch.setattr(om, "position_size_reason", lambda ticker, side, qty: None)
    monkeypatch.setattr(om, "position_count_reason", lambda ticker, side: None)
    monkeypatch.setattr(om, "short_sale_reason", lambda ticker, side, qty: None)
    monkeypatch.setattr(om, "stale_recommendation_reason", lambda rec: None)


def _approved(qty=10):
    return lambda ticker, checkpoint: {
        "decision": "approve",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "terms": {"qty": qty},
    }


def test_kill_switch_off_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(False))
    with pytest.raises(om.OrderRefused, match="Kill switch"):
        om.submit_approved_order(REC, qty=1)


def test_no_decision_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", lambda ticker, checkpoint: None)
    with pytest.raises(om.OrderRefused, match="No approval record"):
        om.submit_approved_order(REC, qty=1)


def test_rejected_decision_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(
        om, "get_decision",
        lambda ticker, checkpoint: {"decision": "reject", "timestamp": datetime.now(timezone.utc).isoformat(), "terms": {}},
    )
    with pytest.raises(om.OrderRefused, match="not approve"):
        om.submit_approved_order(REC, qty=1)


def test_expired_approval_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    monkeypatch.setattr(
        om, "get_decision",
        lambda ticker, checkpoint: {"decision": "approve", "timestamp": old_ts, "terms": {}},
    )
    monkeypatch.setattr(om, "is_expired", lambda ts: True)
    with pytest.raises(om.OrderRefused, match="expired"):
        om.submit_approved_order(REC, qty=1)


def test_qty_mismatch_beyond_tolerance_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(
        om, "get_decision",
        lambda ticker, checkpoint: {
            "decision": "approve",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "terms": {"qty": 10},
        },
    )
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    with pytest.raises(om.OrderRefused, match="does not match approved qty"):
        om.submit_approved_order(REC, qty=100)


def test_matching_approval_submits(monkeypatch, tmp_path):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: {"id": "fake", "symbol": t, "qty": q})
    monkeypatch.setattr(om, "TRADES_DIR", tmp_path)

    order = om.submit_approved_order(REC, qty=10)
    assert order["id"] == "fake"


def test_trade_record_tags_source_human_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: {"id": "fake", "symbol": t, "qty": q})
    monkeypatch.setattr(om, "TRADES_DIR", tmp_path)

    om.submit_approved_order(REC, qty=10)
    written = list(tmp_path.rglob("orders_submitted.json"))[0]
    record = json.loads(written.read_text())[0]
    assert record["source"] == "human"


def test_trade_record_tags_source_auto_when_passed(monkeypatch, tmp_path):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: {"id": "fake", "symbol": t, "qty": q})
    monkeypatch.setattr(om, "TRADES_DIR", tmp_path)

    om.submit_approved_order(REC, qty=10, source="auto")
    written = list(tmp_path.rglob("orders_submitted.json"))[0]
    record = json.loads(written.read_text())[0]
    assert record["source"] == "auto"

    written = list(tmp_path.rglob("orders_submitted.json"))
    assert len(written) == 1


def test_options_contract_refused_before_any_approval_lookup(monkeypatch):
    """The options ban must not be satisfiable by approving the contract."""
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))

    def fail(*args, **kwargs):
        raise AssertionError("should have been refused before the approval lookup")

    monkeypatch.setattr(om, "get_decision", fail)
    option = {"ticker": "AAPL240119C00150000", "checkpoint": "midday", "action": "BUY"}
    with pytest.raises(om.OrderRefused, match="never trades options"):
        om.submit_approved_order(option, qty=1)


def test_daily_loss_breach_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(om, "daily_loss_reason", lambda: "Daily loss -2.40% has reached the 2.0% cap")
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: pytest.fail("must not submit"))

    with pytest.raises(om.OrderRefused, match="2.0% cap"):
        om.submit_approved_order(REC, qty=10)


def test_stale_recommendation_breach_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(
        om,
        "stale_recommendation_reason",
        lambda rec: "TSLA's price has moved 5.20% since this recommendation was scored",
    )
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: pytest.fail("must not submit"))

    with pytest.raises(om.OrderRefused, match="price has moved"):
        om.submit_approved_order(REC, qty=10)


def test_stale_recommendation_checked_before_risk_cap_guardrails(monkeypatch):
    """Staleness is checked first among the account-state guardrails — no
    point evaluating risk caps against an intent the market has already
    moved past."""
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(om, "stale_recommendation_reason", lambda rec: "stale — re-run the checkpoint")

    def fail(*args, **kwargs):
        raise AssertionError("should have been refused before reaching position_size_reason")

    monkeypatch.setattr(om, "position_size_reason", fail)
    with pytest.raises(om.OrderRefused, match="stale"):
        om.submit_approved_order(REC, qty=10)


def test_position_size_breach_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(
        om, "position_size_reason", lambda ticker, side, qty: "TSLA would reach 9.10% ... over the 5.0% cap"
    )
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: pytest.fail("must not submit"))

    with pytest.raises(om.OrderRefused, match="over the 5.0% cap"):
        om.submit_approved_order(REC, qty=10)


def test_daily_trade_count_breach_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(
        om, "daily_trade_count_reason", lambda: "Daily trade cap reached: 5 of 5 orders already submitted today"
    )
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: pytest.fail("must not submit"))

    with pytest.raises(om.OrderRefused, match="Daily trade cap reached"):
        om.submit_approved_order(REC, qty=10)


def test_daily_trade_count_checked_before_approval_lookup(monkeypatch):
    """Cheap/local check — must refuse before ever consulting the approval record."""
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "daily_trade_count_reason", lambda: "Daily trade cap reached: 5 of 5")

    def fail(*args, **kwargs):
        raise AssertionError("should have been refused before the approval lookup")

    monkeypatch.setattr(om, "get_decision", fail)
    with pytest.raises(om.OrderRefused, match="Daily trade cap reached"):
        om.submit_approved_order(REC, qty=10)


def test_position_count_breach_refuses(monkeypatch):
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(
        om,
        "position_count_reason",
        lambda ticker, side: "Opening TSLA would exceed the 10-position concurrent-positions cap",
    )
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: pytest.fail("must not submit"))

    with pytest.raises(om.OrderRefused, match="concurrent-positions cap"):
        om.submit_approved_order(REC, qty=10)


def test_short_sale_breach_refuses(monkeypatch):
    sell_rec = {"ticker": "TSLA", "checkpoint": "midday", "action": "SELL"}
    monkeypatch.setattr(om, "load_risk_limits", lambda: _risk(True))
    monkeypatch.setattr(om, "get_decision", _approved(qty=10))
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(
        om, "short_sale_reason", lambda ticker, side, qty: "SELL 10 TSLA refused: never opens short positions."
    )
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: pytest.fail("must not submit"))

    with pytest.raises(om.OrderRefused, match="never opens short positions"):
        om.submit_approved_order(sell_rec, qty=10)

"""Safety-critical: order_manager must refuse to submit without an approval
record matching, and must always respect the kill switch (plan §8, §9).
"""

from datetime import datetime, timedelta, timezone

import pytest

from trading_agent.execute import order_manager as om


REC = {"ticker": "TSLA", "checkpoint": "midday", "action": "BUY"}


def _risk(trading_enabled: bool):
    return {"operational": {"trading_enabled": trading_enabled}}


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
    monkeypatch.setattr(
        om, "get_decision",
        lambda ticker, checkpoint: {
            "decision": "approve",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "terms": {"qty": 10},
        },
    )
    monkeypatch.setattr(om, "is_expired", lambda ts: False)
    monkeypatch.setattr(om, "submit_market_order", lambda t, s, q: {"id": "fake", "symbol": t, "qty": q})
    monkeypatch.setattr(om, "TRADES_DIR", tmp_path)

    order = om.submit_approved_order(REC, qty=10)
    assert order["id"] == "fake"

    written = list(tmp_path.rglob("orders_submitted.json"))
    assert len(written) == 1

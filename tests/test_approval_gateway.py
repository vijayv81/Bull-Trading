from datetime import datetime, timedelta, timezone

import pytest

from trading_agent.notify import approval_gateway as gw


@pytest.fixture(autouse=True)
def isolated_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "RECOMMENDATIONS_DIR", tmp_path / "recommendations")
    monkeypatch.setattr(gw, "APPROVALS_DIR", tmp_path / "approvals")
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": "console"}})
    monkeypatch.setattr(gw, "load_risk_limits", lambda: {"operational": {"approval_expiry_hours": 2}})


def test_record_and_get_decision_roundtrip():
    gw.record_decision("TSLA", "midday", "approve", {"qty": 5})
    decision = gw.get_decision("TSLA", "midday")
    assert decision["decision"] == "approve"
    assert decision["terms"]["qty"] == 5


def test_get_decision_missing_returns_none():
    assert gw.get_decision("NOPE", "midday") is None


def test_latest_decision_wins_on_repeat():
    gw.record_decision("TSLA", "midday", "approve")
    gw.record_decision("TSLA", "midday", "reject")
    assert gw.get_decision("TSLA", "midday")["decision"] == "reject"


def test_is_expired_true_past_window():
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    assert gw.is_expired(old_ts) is True


def test_is_expired_false_within_window():
    recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert gw.is_expired(recent_ts) is False


def test_list_pending_excludes_decided_tickers():
    gw.save_recommendation({"ticker": "TSLA", "checkpoint": "midday", "action": "BUY", "confidence": 70})
    gw.save_recommendation({"ticker": "GOOG", "checkpoint": "midday", "action": "HOLD", "confidence": 40})
    gw.record_decision("TSLA", "midday", "approve")

    pending = gw.list_pending("midday")
    assert [r["ticker"] for r in pending] == ["GOOG"]

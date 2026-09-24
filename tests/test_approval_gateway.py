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


def test_notification_channels_normalizes_string_and_list(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": "console"}})
    assert gw.notification_channels() == {"console"}
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["console", "email"]}})
    assert gw.notification_channels() == {"console", "email"}


def test_notify_digest_skips_when_no_email_or_sms_channel(monkeypatch):
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append("email"))
    gw.notify_digest([{"ticker": "TSLA", "action": "BUY", "confidence": 70}], "midday")
    assert calls == []


def test_notify_digest_sends_email_and_suppresses_sms_when_nothing_actionable(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email", "sms"]}})
    email_calls = []
    sms_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))
    monkeypatch.setattr("trading_agent.notify.senders.send_sms", lambda *a, **k: sms_calls.append(a))

    gw.notify_digest([{"ticker": "GOOG", "action": "HOLD", "confidence": 40}], "midday")

    assert len(email_calls) == 1
    assert "No actionable" in email_calls[0][1]
    assert sms_calls == []


def test_notify_digest_sends_sms_when_actionable(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email", "sms"]}})
    sms_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: None)
    monkeypatch.setattr("trading_agent.notify.senders.send_sms", lambda *a, **k: sms_calls.append(a))

    gw.notify_digest([{"ticker": "TSLA", "action": "BUY", "confidence": 70}], "midday")

    assert len(sms_calls) == 1
    assert "TSLA" in sms_calls[0][0]


def test_notify_digest_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})

    def boom(*a, **k):
        raise RuntimeError("RESEND_API_KEY not set")

    monkeypatch.setattr("trading_agent.notify.senders.send_email", boom)
    gw.notify_digest([{"ticker": "TSLA", "action": "BUY", "confidence": 70}], "midday")  # must not raise


def test_notify_digest_includes_auto_apply_section(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email", "sms"]}})
    email_calls = []
    sms_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))
    monkeypatch.setattr("trading_agent.notify.senders.send_sms", lambda *a, **k: sms_calls.append(a))

    auto_results = [
        {"ticker": "TSLA", "status": "submitted", "qty": 12},
        {"ticker": "GOOG", "status": "refused", "reason": "over the 5.0% cap"},
    ]
    gw.notify_digest(
        [{"ticker": "TSLA", "action": "BUY", "confidence": 90}], "pre_open", auto_results=auto_results
    )

    email_body = email_calls[0][1]
    assert "Auto-applied" in email_body
    assert "TSLA: SUBMITTED qty=12" in email_body
    assert "GOOG: refused" in email_body
    assert "AUTO: TSLA x12" in sms_calls[0][0]


def test_notify_digest_includes_failed_tickers_section(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    gw.notify_digest(
        [{"ticker": "TSLA", "action": "BUY", "confidence": 90}],
        "pre_open",
        failed_tickers=[{"ticker": "BAD", "error": "ReadTimeout"}],
    )

    body = email_calls[0][1]
    assert "Research failed" in body
    assert "BAD: ReadTimeout" in body


def test_notify_digest_auto_results_alone_still_sms_when_no_actionable(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["sms"]}})
    sms_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_sms", lambda *a, **k: sms_calls.append(a))

    gw.notify_digest([], "pre_open", auto_results=[{"ticker": "TSLA", "status": "submitted", "qty": 5}])
    assert len(sms_calls) == 1


# --- notify_daily_summary -----------------------------------------------------


def _summary(**overrides):
    base = {
        "day": "2026-09-25",
        "portfolio_return_pct": 1.2,
        "benchmark_symbol": "SPY",
        "benchmark_return_pct": 0.5,
        "outperformance_pct": 0.7,
        "recommendations_count": 3,
        "trades_count": 2,
        "journal_entries": [],
    }
    base.update(overrides)
    return base


def test_daily_summary_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(
        gw, "load_agent_config",
        lambda: {"notifications": {"channel": ["email"], "daily_summary_enabled": False}},
    )
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_daily_summary(_summary())
    assert calls == []


def test_daily_summary_skipped_when_no_email_or_sms_channel(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["console"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_daily_summary(_summary())
    assert calls == []


def test_daily_summary_email_includes_benchmark_comparison(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_daily_summary(_summary())

    body = calls[0][1]
    assert "Portfolio: +1.20%" in body
    assert "SPY: +0.50%" in body
    assert "Vs. SPY: +0.70 pts" in body


def test_daily_summary_includes_journal_findings(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    entry = {
        "ticker": "TSLA", "checkpoint": "midday", "decision": "approve",
        "reasoning": "held support at 240",
        "outcome": {"directionally_correct": True},
    }
    gw.notify_daily_summary(_summary(journal_entries=[entry]))

    body = calls[0][1]
    assert "TSLA [midday] approve — correct: held support at 240" in body


def test_daily_summary_handles_missing_returns_gracefully(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_daily_summary(_summary(portfolio_return_pct=None, benchmark_return_pct=None, outperformance_pct=None))

    body = calls[0][1]
    assert "unavailable" in body


def test_daily_summary_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})

    def boom(*a, **k):
        raise RuntimeError("RESEND_API_KEY not set")

    monkeypatch.setattr("trading_agent.notify.senders.send_email", boom)
    gw.notify_daily_summary(_summary())  # must not raise

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


def test_notify_digest_caps_headlined_proposals_per_checkpoint(monkeypatch):
    monkeypatch.setattr(
        gw, "load_agent_config",
        lambda: {"notifications": {"channel": ["email", "sms"]}},
    )
    monkeypatch.setattr(
        gw, "load_risk_limits",
        lambda: {"portfolio": {"max_new_proposals_per_checkpoint": 2}},
    )
    email_calls = []
    sms_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))
    monkeypatch.setattr("trading_agent.notify.senders.send_sms", lambda *a, **k: sms_calls.append(a))

    recs = [
        {"ticker": "LOW", "action": "BUY", "confidence": 55, "rationale": "low conviction setup"},
        {"ticker": "HIGH", "action": "BUY", "confidence": 95, "rationale": "strong breakout"},
        {"ticker": "MID", "action": "BUY", "confidence": 75, "rationale": "moderate setup"},
    ]
    gw.notify_digest(recs, "midday")

    subject, body = email_calls[0][:2]
    assert "2 recommendation(s) (top 2 of 3)" in subject
    assert "HIGH" in body and "MID" in body
    assert "LOW" not in body.split("more actionable")[0]  # not in the headlined list
    assert "1 more actionable recommendation" in body
    assert "HIGH" in sms_calls[0][0] and "MID" in sms_calls[0][0]
    assert "LOW" not in sms_calls[0][0]


def test_notify_digest_no_cap_configured_shows_everything(monkeypatch):
    monkeypatch.setattr(
        gw, "load_agent_config",
        lambda: {"notifications": {"channel": ["email"]}},
    )
    monkeypatch.setattr(gw, "load_risk_limits", lambda: {"portfolio": {}})
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    recs = [
        {"ticker": "LOW", "action": "BUY", "confidence": 55, "rationale": "low conviction setup"},
        {"ticker": "HIGH", "action": "BUY", "confidence": 95, "rationale": "strong breakout"},
        {"ticker": "MID", "action": "BUY", "confidence": 75, "rationale": "moderate setup"},
    ]
    gw.notify_digest(recs, "midday")

    subject, body = email_calls[0][:2]
    assert "(top" not in subject
    assert all(t in body for t in ("LOW", "HIGH", "MID"))


def test_notify_digest_silent_when_nothing_actionable(monkeypatch):
    """Per user instruction 2026-09-30 ("crisp ... only show actionable
    items"): a checkpoint with nothing actionable, nothing auto-applied, and
    no data-quality problem sends nothing at all, on either channel — same
    "silence is fine on a quiet run" convention as notify_pending_reminder().
    """
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email", "sms"]}})
    email_calls = []
    sms_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))
    monkeypatch.setattr("trading_agent.notify.senders.send_sms", lambda *a, **k: sms_calls.append(a))

    gw.notify_digest([{"ticker": "GOOG", "action": "HOLD", "confidence": 40}], "midday")

    assert email_calls == []
    assert sms_calls == []


def test_notify_digest_sends_sms_when_actionable(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email", "sms"]}})
    sms_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: None)
    monkeypatch.setattr("trading_agent.notify.senders.send_sms", lambda *a, **k: sms_calls.append(a))

    gw.notify_digest(
        [{"ticker": "TSLA", "action": "BUY", "confidence": 70, "rationale": "breakout above resistance"}], "midday"
    )

    assert len(sms_calls) == 1
    assert "TSLA" in sms_calls[0][0]


def test_notify_digest_skips_a_recommendation_with_no_analysis(monkeypatch):
    """Per user instruction 2026-09-30: a stock with no real analysis behind
    it (blank rationale) is skipped from the notification entirely, not
    shown as an empty-looking entry."""
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    recs = [
        {"ticker": "NOINFO", "action": "BUY", "confidence": 90, "rationale": ""},
        {"ticker": "GOODINFO", "action": "BUY", "confidence": 80, "rationale": "clear breakout setup"},
    ]
    gw.notify_digest(recs, "midday")

    body = email_calls[0][1]
    assert "GOODINFO" in body
    assert "NOINFO" not in body


def test_notify_digest_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})

    def boom(*a, **k):
        raise RuntimeError("RESEND_API_KEY not set")

    monkeypatch.setattr("trading_agent.notify.senders.send_email", boom)
    gw.notify_digest(
        [{"ticker": "TSLA", "action": "BUY", "confidence": 70, "rationale": "breakout above resistance"}], "midday"
    )  # must not raise


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


def test_notify_digest_never_shows_failed_tickers(monkeypatch):
    """Per user instruction 2026-09-30 ("only show actionable items"):
    research failures aren't actionable and are already visible in the
    checkpoint session's own console output — the digest itself never
    mentions them, whether or not something else is actionable."""
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    gw.notify_digest(
        [{"ticker": "TSLA", "action": "BUY", "confidence": 90, "rationale": "breakout above resistance"}],
        "pre_open",
        failed_tickers=[{"ticker": "BAD", "error": "ReadTimeout"}],
    )

    body = email_calls[0][1]
    assert "TSLA" in body
    assert "Research failed" not in body
    assert "BAD" not in body


def test_notify_digest_failed_tickers_alone_sends_nothing(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    gw.notify_digest([], "pre_open", failed_tickers=[{"ticker": "BAD", "error": "ReadTimeout"}])

    assert email_calls == []


def test_notify_digest_sends_mobile_friendly_html_body(monkeypatch):
    """Per user instruction 2026-09-30 ("crisp and mobile friendly"):
    the email carries an HTML rendering alongside the plain-text body."""
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    gw.notify_digest(
        [{"ticker": "TSLA", "action": "BUY", "confidence": 91, "rationale": "breakout above resistance"}],
        "pre_open",
    )

    subject, body, html_body = email_calls[0]
    assert html_body is not None
    assert "TSLA" in html_body
    assert "BUY" in html_body
    assert "max-width:600px" in html_body  # mobile-width single column


def test_notify_digest_html_includes_data_quality_alert(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    gw.notify_digest([], "pre_open", data_quality_alert="every ticker scored an identical confidence")

    html_body = email_calls[0][2]
    assert "identical confidence" in html_body


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


def test_daily_summary_includes_learning_and_optimization_section(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_daily_summary(
        _summary(
            optimization_proposals=["Reduce sentiment weight from 0.2 to 0.1 (hit rate 32% vs technical's 61%)"],
            signal_hit_rates={"technical": {"hit_rate": 0.61, "n": 20}, "sentiment": {"hit_rate": 0.32, "n": 18}},
            learning_outcome="If incorporated, future scoring would lean more on technical and less on sentiment.",
            scoring_weights={"technical": 0.4, "sentiment": 0.1},
        )
    )

    plain_body, html_body = calls[0][1], calls[0][2]
    assert "Learning & optimization:" in plain_body
    assert "Reduce sentiment weight" in plain_body
    assert "technical 61%" in plain_body and "sentiment 32%" in plain_body
    assert "If incorporated" in plain_body
    assert "Weights currently in effect: technical 0.4, sentiment 0.1" in plain_body
    assert "never applied automatically" in plain_body

    assert "Learning" in html_body and "Optimization" in html_body
    assert "Reduce sentiment weight" in html_body
    assert "If incorporated" in html_body
    assert "never applied automatically" in html_body


def test_daily_summary_learning_section_empty_when_no_history(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_daily_summary(_summary())

    plain_body, html_body = calls[0][1], calls[0][2]
    assert "Learning & optimization:" in plain_body
    assert "never applied automatically" in plain_body
    assert "No weight-adjustment proposal yet" in html_body


def test_daily_summary_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})

    def boom(*a, **k):
        raise RuntimeError("RESEND_API_KEY not set")

    monkeypatch.setattr("trading_agent.notify.senders.send_email", boom)
    gw.notify_daily_summary(_summary())  # must not raise


# --- notify_weekly_learning_review --------------------------------------------


def _review(**overrides):
    base = {
        "window_start": "2026-09-03",
        "window_end": "2026-10-02",
        "lookback_days": 30,
        "trades_count": 2,
        "realized_pnl": {"total": 150.0, "by_symbol": {"TSLA": 150.0}, "pending_fills": 0},
        "unrealized_pnl": {"positions": [], "total": -25.0, "error": None},
        "portfolio_status": {"equity": 100000.0, "cash": 50000.0, "buying_power": 50000.0, "error": None},
        "actionable_count": 5,
        "executed_count": 2,
        "never_decided": [],
        "approved_not_executed": [],
        "rejected": [],
        "auto_apply_attempts_by_status": {"submitted": 2, "refused": 0, "skipped": 0, "error": 0},
        "refusal_breakdown": {},
        "missed_opportunities": [],
        "signal_hit_rates": {},
        "optimization_proposals": ["No performance history yet — nothing to propose."],
        "scoring_weights": {},
        "learning_outcome": None,
        "config_tuning_notes": [],
    }
    base.update(overrides)
    return base


def test_weekly_learning_review_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(
        gw, "load_agent_config",
        lambda: {"notifications": {"channel": ["email"], "weekly_learning_review_enabled": False}},
    )
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_weekly_learning_review(_review())
    assert calls == []


def test_weekly_learning_review_skipped_when_no_email_or_sms_channel(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["console"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_weekly_learning_review(_review())
    assert calls == []


def test_weekly_learning_review_includes_trades_and_portfolio_status(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_weekly_learning_review(_review())

    subject, plain_body, html_body = calls[0][0], calls[0][1], calls[0][2]
    assert "last 30 days (2026-09-03 to 2026-10-02)" in subject
    assert "last 30 days (2026-09-03 to 2026-10-02)" in plain_body
    assert "Trades executed: 2" in plain_body
    assert "Realized P&L: $150.00" in plain_body
    assert "equity $100,000.00" in plain_body
    assert "Trades executed" in html_body
    assert "Portfolio status" in html_body
    assert "last 30 days" in html_body


def test_weekly_learning_review_includes_orders_not_placed_and_refusal_breakdown(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_weekly_learning_review(
        _review(
            never_decided=[{"ticker": "AAA"}],
            approved_not_executed=[{"ticker": "BBB"}],
            auto_outcome_unlogged=[{"ticker": "DDD"}, {"ticker": "EEE"}],
            rejected=[{"ticker": "CCC"}],
            auto_apply_attempts_by_status={"submitted": 1, "refused": 2, "skipped": 1, "error": 0},
            refusal_breakdown={"stale: price drift": 2},
            attempt_log_span="3 of the last 30 days, since 2026-09-30",
        )
    )

    plain_body, html_body = calls[0][1], calls[0][2]
    assert "1 never decided" in plain_body
    assert "1 human-approved but never submitted" in plain_body
    assert "2 auto-apply with no logged outcome" in plain_body
    assert "1 human-rejected" in plain_body
    assert "Refusal breakdown (3 of the last 30 days, since 2026-09-30):" in plain_body
    assert "stale: price drift: 2" in plain_body
    assert "Orders not placed" in html_body
    assert "auto-apply, outcome not logged" in html_body
    assert "3 of the last 30 days, since 2026-09-30" in html_body
    assert "stale: price drift" in html_body


def test_weekly_learning_review_includes_missed_opportunities_hindsight(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_weekly_learning_review(
        _review(
            missed_opportunities=[
                {
                    "ticker": "WON", "action": "BUY", "confidence": 90, "category": "never decided",
                    "reference_price": 100.0, "current_price": 110.0, "pct_change": 10.0, "would_have_helped": True,
                },
                {
                    "ticker": "LOST", "action": "BUY", "confidence": 90, "category": "auto-refused: stale: price drift",
                    "reference_price": 100.0, "current_price": 90.0, "pct_change": -10.0, "would_have_helped": False,
                },
            ]
        )
    )

    plain_body, html_body = calls[0][1], calls[0][2]
    assert "WON BUY" in plain_body and "would have helped" in plain_body
    assert "LOST BUY" in plain_body and "would NOT have helped" in plain_body
    assert "WOULD HAVE HELPED" in html_body
    assert "AVOIDED CORRECTLY" in html_body


def test_weekly_learning_review_includes_learning_and_optimization(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_weekly_learning_review(
        _review(
            optimization_proposals=["Reduce sentiment weight (hit rate 32% vs technical's 61%)"],
            signal_hit_rates={"technical": {"hit_rate": 0.61, "n": 20}},
            learning_outcome="If incorporated, scoring would lean more on technical.",
            scoring_weights={"technical": 0.4},
            config_tuning_notes=["3 of 5 refusals this week were stale blocks."],
        )
    )

    plain_body, html_body = calls[0][1], calls[0][2]
    assert "Reduce sentiment weight" in plain_body
    assert "technical: 61%" in plain_body
    assert "If incorporated" in plain_body
    assert "Weights currently in effect: technical 0.4" in plain_body
    assert "3 of 5 refusals" in plain_body
    assert "never applied automatically" in plain_body
    assert "Reduce sentiment weight" in html_body
    assert "Other observations for next week" in html_body


def test_weekly_learning_review_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})

    def boom(*a, **k):
        raise RuntimeError("RESEND_API_KEY not set")

    monkeypatch.setattr("trading_agent.notify.senders.send_email", boom)
    gw.notify_weekly_learning_review(_review())  # must not raise


# --- pending_approvals_today / notify_pending_reminder -------------------------


def _rec(ticker, checkpoint, action="BUY", confidence=70, age_hours=0.0):
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    return {
        "ticker": ticker, "checkpoint": checkpoint, "action": action,
        "confidence": confidence, "timestamp": ts, "rationale": "test",
    }


def test_pending_approvals_today_spans_all_checkpoints():
    gw.save_recommendation(_rec("TSLA", "pre_open"))
    gw.save_recommendation(_rec("GOOG", "midday"))

    pending = gw.pending_approvals_today()
    assert {r["ticker"] for r in pending} == {"TSLA", "GOOG"}


def test_pending_approvals_today_excludes_decided():
    gw.save_recommendation(_rec("TSLA", "pre_open"))
    gw.record_decision("TSLA", "pre_open", "approve")

    assert gw.pending_approvals_today() == []


def test_pending_approvals_today_excludes_hold():
    gw.save_recommendation(_rec("TSLA", "pre_open", action="HOLD"))
    assert gw.pending_approvals_today() == []


def test_pending_approvals_today_flags_expired_but_still_includes_it():
    gw.save_recommendation(_rec("TSLA", "pre_open", age_hours=3.0))  # past the 2h window
    pending = gw.pending_approvals_today()
    assert [r["ticker"] for r in pending] == ["TSLA"]
    assert pending[0]["expired"] is True


def test_pending_approvals_today_flags_unexpired_as_not_expired():
    gw.save_recommendation(_rec("TSLA", "pre_open", age_hours=0.5))
    pending = gw.pending_approvals_today()
    assert [r["ticker"] for r in pending] == ["TSLA"]
    assert pending[0]["expired"] is False


def test_notify_pending_reminder_silent_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_pending_reminder()
    assert calls == []


def test_notify_pending_reminder_skips_when_no_email_or_sms_channel(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["console"]}})
    gw.save_recommendation(_rec("TSLA", "pre_open"))
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_pending_reminder()
    assert calls == []


def test_notify_pending_reminder_sends_email_listing_pending(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email", "sms"]}})
    gw.save_recommendation(_rec("TSLA", "pre_open"))
    gw.save_recommendation(_rec("GOOG", "midday", action="SELL", confidence=85))
    email_calls = []
    sms_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))
    monkeypatch.setattr("trading_agent.notify.senders.send_sms", lambda *a, **k: sms_calls.append(a))

    gw.notify_pending_reminder()

    assert len(email_calls) == 1
    subject, body = email_calls[0]
    assert "2 pending" in subject
    assert "expired" not in subject
    assert "TSLA [pre_open]" in body
    assert "GOOG [midday]" in body
    assert "expired" not in body
    assert len(sms_calls) == 1
    assert "2 pending" in sms_calls[0][0]


def test_notify_pending_reminder_separates_expired_from_still_actionable(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    gw.save_recommendation(_rec("TSLA", "pre_open", age_hours=3.0))  # expired
    gw.save_recommendation(_rec("GOOG", "midday", action="SELL", confidence=85))  # fresh
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    gw.notify_pending_reminder()

    subject, body = email_calls[0]
    assert "1 pending" in subject
    assert "1 expired" in subject
    assert "still awaiting a decision" in body
    assert "GOOG [midday]" in body.split("expired today")[0]
    assert "expired today with no decision" in body
    assert "TSLA [pre_open]" in body.split("expired today")[1]


def test_notify_pending_reminder_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    gw.save_recommendation(_rec("TSLA", "pre_open"))

    def boom(*a, **k):
        raise RuntimeError("RESEND_API_KEY not set")

    monkeypatch.setattr("trading_agent.notify.senders.send_email", boom)
    gw.notify_pending_reminder()  # must not raise


# --- notify_weekly_report -------------------------------------------------------


_WEEKLY_REPORT_TEXT = """# Weekly Report — week of 2026-09-21

## Summary
- Total recommendations: 12
- Approved: 5 | Rejected: 2 | Expired: 5
- Trades executed (paper): 5

## P&L
- Realized this week: $123.45
- Unrealized (live, open positions right now): $67.89

## Daily breakdown
- 2026-09-22: 4 recommendations, 2 approved, 1 rejected, 1 expired, 2 trades
"""


def test_notify_weekly_report_skips_when_no_email_or_sms_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["console"]}})
    report_path = tmp_path / "2026-W39.md"
    report_path.write_text(_WEEKLY_REPORT_TEXT)
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))

    gw.notify_weekly_report(report_path)
    assert calls == []


def test_notify_weekly_report_sends_full_markdown_by_email(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    report_path = tmp_path / "2026-W39.md"
    report_path.write_text(_WEEKLY_REPORT_TEXT)
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    gw.notify_weekly_report(report_path)

    assert len(email_calls) == 1
    subject, body, html_body = email_calls[0]
    assert "2026-W39" in subject
    assert body == _WEEKLY_REPORT_TEXT
    assert "Realized this week: $123.45" in body
    assert "Realized this week: $123.45" in html_body


def test_notify_weekly_report_html_makes_markdown_links_clickable(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    report_path = tmp_path / "2026-W40.md"
    report_path.write_text(
        "# Weekly Report — week of 2026-09-28\n\n"
        "## Proposed model/config improvements\n"
        "- **Raise the stale-price limit** (loosens a guardrail): `execution.max_price_drift_pct` 3.0 → 4.0. "
        "[Review & apply](https://claude.ai/artifact/X#opt-1.apply.loosens) · "
        "[Dismiss](https://claude.ai/artifact/X#opt-1.dismiss.loosens)\n"
        "_No other options <b>today</b>._\n"
    )
    email_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: email_calls.append(a))

    gw.notify_weekly_report(report_path)

    _, body, html_body = email_calls[0]
    assert "[Review & apply](https://claude.ai/artifact/X#opt-1.apply.loosens)" in body
    assert '<a href="https://claude.ai/artifact/X#opt-1.apply.loosens"' in html_body
    assert ">Review &amp; apply</a>" in html_body
    assert '<a href="https://claude.ai/artifact/X#opt-1.dismiss.loosens"' in html_body
    assert "<b>Raise the stale-price limit</b>" in html_body
    assert "&lt;b&gt;today&lt;/b&gt;" in html_body  # report text is escaped, never trusted as markup


def test_notify_weekly_report_sms_gets_just_the_summary_section(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["sms"]}})
    report_path = tmp_path / "2026-W39.md"
    report_path.write_text(_WEEKLY_REPORT_TEXT)
    sms_calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_sms", lambda *a, **k: sms_calls.append(a))

    gw.notify_weekly_report(report_path)

    assert len(sms_calls) == 1
    body = sms_calls[0][0]
    assert "Total recommendations: 12" in body
    assert "Daily breakdown" not in body  # only the Summary section, not the whole report
    assert len(body) <= 300


def test_notify_weekly_report_send_failure_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    report_path = tmp_path / "2026-W39.md"
    report_path.write_text(_WEEKLY_REPORT_TEXT)

    def boom(*a, **k):
        raise RuntimeError("RESEND_API_KEY not set")

    monkeypatch.setattr("trading_agent.notify.senders.send_email", boom)
    gw.notify_weekly_report(report_path)  # must not raise


# --- report PR link in the weekly learning review email -----------------------

PR_URL = "https://github.com/vijayv81/Bull-Trading/pull/99"


def _send_review(monkeypatch, review, **kwargs):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_weekly_learning_review(review, **kwargs)
    return calls[0]


def test_weekly_learning_review_leads_with_a_clickable_pr_link(monkeypatch):
    subject, plain, html = _send_review(
        monkeypatch,
        _review(optimization_options=[{"id": "o1", "title": "t", "changes": [], "why": "w", "effect": "e"}]),
        pr_url=PR_URL,
    )
    assert PR_URL in plain
    assert plain.index(PR_URL) < plain.index("Optimizations you can apply")  # leads the email
    assert "reports/learning/2026-10-02.md" in plain
    assert "1 optimization option(s)" in plain
    assert "can't take effect until this is merged" in plain
    assert f'href="{PR_URL}"' in html
    assert "Review &amp; approve this report" in html
    assert "PR to approve" in subject


def test_weekly_learning_review_has_no_pr_section_without_a_url(monkeypatch):
    subject, plain, html = _send_review(monkeypatch, _review())
    assert "Review & approve" not in plain
    assert "approve this report" not in html
    assert "PR to approve" not in subject


# --- daily summary: capped rows collapse, "as of" note ------------------------


def test_daily_summary_collapses_capped_attempts_into_one_row_per_checkpoint(monkeypatch):
    capped = [
        {"ticker": t, "status": "capped", "checkpoint": "pre_close",
         "reason": "daily auto-apply cap reached (5 of 5 orders today)"}
        for t in ("IREN", "PDSB", "SDEV")
    ]
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_daily_summary(_summary(auto_apply_attempts=capped))
    plain, html = calls[0][1], calls[0][2]

    assert plain.count("capped") == 1
    assert "3 candidate(s) [pre_close]: capped — not attempted, daily auto-apply cap reached" in plain
    assert "IREN, PDSB, SDEV" in plain
    assert html.count("CAPPED") == 1 and "IREN, PDSB, SDEV" in html


def test_daily_summary_states_when_the_figures_were_taken(monkeypatch):
    note = "Figures as of 15:55 ET — before the 4:00pm ET close, so today's return and outcomes are not final"
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_daily_summary(_summary(as_of_note=note))
    assert note in calls[0][1] and "Figures as of 15:55 ET" in calls[0][2]

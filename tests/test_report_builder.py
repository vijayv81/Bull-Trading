import json
from pathlib import Path

import pytest

from trading_agent.reporting import report_builder as rb
from trading_agent.utils import append_json, day_dir


def _order(symbol, side, filled_qty=None, filled_avg_price=None, submitted_at="2026-09-24T00:00:00+00:00"):
    return {
        "order": {
            "symbol": symbol,
            "side": side,
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price,
        },
        "submitted_at": submitted_at,
    }


# --- _realized_pnl -----------------------------------------------------------


def test_realized_pnl_empty_trades_is_zero():
    result = rb._realized_pnl([])
    assert result == {"by_symbol": {}, "total": 0.0, "pending_fills": 0}


def test_realized_pnl_simple_buy_then_sell():
    trades = [
        _order("TSLA", "buy", 10, 100.0, submitted_at="2026-09-24T09:00:00+00:00"),
        _order("TSLA", "sell", 10, 110.0, submitted_at="2026-09-24T15:00:00+00:00"),
    ]
    result = rb._realized_pnl(trades)
    assert result["by_symbol"]["TSLA"] == 100.0  # (110-100)*10
    assert result["total"] == 100.0
    assert result["pending_fills"] == 0


def test_realized_pnl_average_cost_basis_across_multiple_buys():
    trades = [
        _order("TSLA", "buy", 10, 100.0, submitted_at="2026-09-24T09:00:00+00:00"),
        _order("TSLA", "buy", 10, 120.0, submitted_at="2026-09-24T10:00:00+00:00"),
        # avg cost now (10*100 + 10*120) / 20 = 110
        _order("TSLA", "sell", 20, 130.0, submitted_at="2026-09-24T15:00:00+00:00"),
    ]
    result = rb._realized_pnl(trades)
    assert result["by_symbol"]["TSLA"] == pytest.approx(400.0)  # (130-110)*20


def test_realized_pnl_skips_unconfirmed_fill_as_pending():
    trades = [
        _order("TSLA", "buy", None, None),  # not yet filled
        _order("GOOG", "sell", 5, 50.0),
    ]
    result = rb._realized_pnl(trades)
    assert result["pending_fills"] == 1
    # GOOG sell with no prior buy basis (avg_cost 0) still realizes against a 0 basis
    assert result["by_symbol"]["GOOG"] == 250.0


def test_realized_pnl_separates_symbols():
    trades = [
        _order("TSLA", "buy", 10, 100.0, submitted_at="2026-09-24T09:00:00+00:00"),
        _order("TSLA", "sell", 10, 90.0, submitted_at="2026-09-24T10:00:00+00:00"),
        _order("GOOG", "buy", 5, 200.0, submitted_at="2026-09-24T09:00:00+00:00"),
        _order("GOOG", "sell", 5, 210.0, submitted_at="2026-09-24T10:00:00+00:00"),
    ]
    result = rb._realized_pnl(trades)
    assert result["by_symbol"]["TSLA"] == -100.0
    assert result["by_symbol"]["GOOG"] == 50.0
    assert result["total"] == -50.0


# --- _unrealized_pnl ----------------------------------------------------------


def test_unrealized_pnl_sums_open_positions(monkeypatch):
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_positions",
        lambda: [
            {"symbol": "TSLA", "qty": "10", "avg_entry_price": "100", "current_price": "110",
             "unrealized_pl": "100.0", "unrealized_plpc": "0.1"},
            {"symbol": "GOOG", "qty": "5", "avg_entry_price": "200", "current_price": "190",
             "unrealized_pl": "-50.0", "unrealized_plpc": "-0.05"},
        ],
    )
    result = rb._unrealized_pnl()
    assert result["total"] == 50.0
    assert result["error"] is None
    assert len(result["positions"]) == 2


def test_unrealized_pnl_no_open_positions(monkeypatch):
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_positions", lambda: [])
    result = rb._unrealized_pnl()
    assert result == {"positions": [], "total": 0.0, "error": None}


def test_unrealized_pnl_handles_alpaca_failure_gracefully(monkeypatch):
    def boom():
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_positions", boom)
    result = rb._unrealized_pnl()
    assert result["positions"] == []
    assert result["total"] == 0.0
    assert "alpaca unreachable" in result["error"]


# --- build_weekly_report integration -----------------------------------------


def test_weekly_report_includes_realized_and_unrealized_pnl(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "RECOMMENDATIONS_DIR", tmp_path / "recommendations")
    monkeypatch.setattr(rb, "APPROVALS_DIR", tmp_path / "approvals")
    monkeypatch.setattr(rb, "TRADES_DIR", tmp_path / "trades")
    monkeypatch.setattr(rb, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr("trading_agent.optimizations.OPTIMIZATIONS_DIR", tmp_path / "optimizations")
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_positions", lambda: [])

    day = "2026-09-21"  # a Monday
    trade_dir = tmp_path / "trades" / day
    trade_dir.mkdir(parents=True)
    (trade_dir / "orders_submitted.json").write_text(
        '[{"order": {"symbol": "TSLA", "side": "buy", "filled_qty": 10, "filled_avg_price": 100.0}, '
        '"submitted_at": "2026-09-21T09:00:00+00:00"}, '
        '{"order": {"symbol": "TSLA", "side": "sell", "filled_qty": 10, "filled_avg_price": 120.0}, '
        '"submitted_at": "2026-09-21T15:00:00+00:00"}]'
    )

    path = rb.build_weekly_report(day)
    text = path.read_text()
    assert "## P&L" in text
    assert "Realized this week: $200.00" in text
    assert "TSLA: $200.00" in text
    assert "Unrealized" in text


# --- _portfolio_return_pct / _benchmark_return_pct / build_daily_summary -----


def test_portfolio_return_pct_computes_from_equity_change(monkeypatch):
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account",
        lambda: {"equity": "102000", "last_equity": "100000"},
    )
    assert rb._portfolio_return_pct() == 2.0


def test_portfolio_return_pct_none_when_alpaca_unreachable(monkeypatch):
    def boom():
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", boom)
    assert rb._portfolio_return_pct() is None


def test_portfolio_return_pct_none_on_zero_prior_equity(monkeypatch):
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account",
        lambda: {"equity": "100", "last_equity": "0"},
    )
    assert rb._portfolio_return_pct() is None


def test_benchmark_return_pct_computes_from_bars(monkeypatch):
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_recent_bars",
        lambda symbol, lookback_days=5: [{"close": 500.0}, {"close": 505.0}],
    )
    assert rb._benchmark_return_pct("SPY") == 1.0


def test_benchmark_return_pct_none_with_insufficient_bars(monkeypatch):
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_recent_bars",
        lambda symbol, lookback_days=5: [{"close": 500.0}],
    )
    assert rb._benchmark_return_pct("SPY") is None


def test_benchmark_return_pct_none_when_unreachable(monkeypatch):
    def boom(symbol, lookback_days=5):
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars", boom)
    assert rb._benchmark_return_pct("SPY") is None


def test_build_daily_summary_combines_portfolio_benchmark_and_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "RECOMMENDATIONS_DIR", tmp_path / "recommendations")
    monkeypatch.setattr(rb, "APPROVALS_DIR", tmp_path / "approvals")
    monkeypatch.setattr(rb, "TRADES_DIR", tmp_path / "trades")
    monkeypatch.setattr(rb, "PERFORMANCE_DIR", tmp_path / "performance")
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.PERFORMANCE_DIR", tmp_path / "performance")
    monkeypatch.setattr(rb, "load_agent_config", lambda: {"scoring_weights": {}})
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account",
        lambda: {"equity": "101000", "last_equity": "100000"},
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_recent_bars",
        lambda symbol, lookback_days=5: [{"close": 500.0}, {"close": 495.0}],
    )
    monkeypatch.setattr(
        "trading_agent.journal.load_entries",
        lambda day: [
            {"ticker": "TSLA", "outcome": {"directionally_correct": True}},
            {"ticker": "GOOG", "outcome": None},  # unmarked, excluded
        ],
    )

    summary = rb.build_daily_summary("2026-09-24")
    assert summary["portfolio_return_pct"] == 1.0
    assert summary["benchmark_return_pct"] == -1.0
    assert summary["outperformance_pct"] == 2.0
    assert summary["benchmark_symbol"] == "SPY"
    assert len(summary["journal_entries"]) == 1
    assert summary["journal_entries"][0]["ticker"] == "TSLA"


def test_build_daily_summary_reports_everything_done_during_the_day(monkeypatch, tmp_path):
    """Per user instruction 2026-09-30 ("capture everything done during the
    day by the agent"): per-checkpoint breakdown, approvals (human vs auto),
    executed trades, and every auto-apply attempt (not just successes)."""
    monkeypatch.setattr(rb, "RECOMMENDATIONS_DIR", tmp_path / "recommendations")
    monkeypatch.setattr(rb, "APPROVALS_DIR", tmp_path / "approvals")
    monkeypatch.setattr(rb, "TRADES_DIR", tmp_path / "trades")
    monkeypatch.setattr(rb, "PERFORMANCE_DIR", tmp_path / "performance")
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.PERFORMANCE_DIR", tmp_path / "performance")
    monkeypatch.setattr(rb, "load_agent_config", lambda: {"scoring_weights": {}})
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account",
        lambda: {"equity": "100000", "last_equity": "100000"},
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_recent_bars",
        lambda symbol, lookback_days=5: [{"close": 500.0}, {"close": 500.0}],
    )
    monkeypatch.setattr("trading_agent.journal.load_entries", lambda day: [])

    day = "2026-09-24"
    append_json(
        day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json",
        {"ticker": "TSLA", "checkpoint": "pre_open", "action": "BUY", "confidence": 90},
    )
    append_json(
        day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json",
        {"ticker": "GOOG", "checkpoint": "pre_open", "action": "HOLD", "confidence": 40},
    )
    append_json(
        day_dir(rb.APPROVALS_DIR, day) / "decisions_pre_open.json",
        {"ticker": "TSLA", "decision": "approve", "terms": {"source": "auto", "qty": 5}},
    )
    append_json(
        day_dir(rb.APPROVALS_DIR, day) / "decisions_pre_open.json",
        {"ticker": "GOOG", "decision": "reject", "terms": {}},
    )
    append_json(
        day_dir(rb.TRADES_DIR, day) / "orders_submitted.json",
        {
            "order": {"symbol": "TSLA", "side": "buy", "qty": 5},
            "source": "auto",
            "recommendation": {"checkpoint": "pre_open"},
        },
    )
    append_json(
        day_dir(rb.TRADES_DIR, day) / "auto_apply_attempts_pre_open.json",
        {"ticker": "TSLA", "status": "submitted", "qty": 5, "checkpoint": "pre_open"},
    )
    append_json(
        day_dir(rb.TRADES_DIR, day) / "auto_apply_attempts_pre_open.json",
        {"ticker": "GOOG", "status": "refused", "reason": "over the 5.0% cap", "checkpoint": "pre_open"},
    )

    summary = rb.build_daily_summary(day)

    assert summary["by_checkpoint"] == {"pre_open": {"total": 2, "buy": 1, "sell": 0, "hold": 1}}
    assert summary["approvals"] == {"approved": 1, "rejected": 1, "auto": 1, "human": 1}
    assert summary["trades"] == [
        {"ticker": "TSLA", "side": "buy", "qty": 5, "source": "auto", "checkpoint": "pre_open"}
    ]
    statuses = {a["ticker"]: a["status"] for a in summary["auto_apply_attempts"]}
    assert statuses == {"TSLA": "submitted", "GOOG": "refused"}


# --- optimization_proposals / scoring_weights / learning_outcome -------------


def _isolate_daily_summary_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "RECOMMENDATIONS_DIR", tmp_path / "recommendations")
    monkeypatch.setattr(rb, "APPROVALS_DIR", tmp_path / "approvals")
    monkeypatch.setattr(rb, "TRADES_DIR", tmp_path / "trades")
    monkeypatch.setattr(rb, "PERFORMANCE_DIR", tmp_path / "performance")
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.PERFORMANCE_DIR", tmp_path / "performance")
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account",
        lambda: {"equity": "100000", "last_equity": "100000"},
    )
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_recent_bars",
        lambda symbol, lookback_days=5: [{"close": 500.0}, {"close": 500.0}],
    )
    monkeypatch.setattr("trading_agent.journal.load_entries", lambda day: [])


def test_build_daily_summary_reports_current_scoring_weights(monkeypatch, tmp_path):
    """Per user instruction 2026-10-01 ("if they were incorporated"):
    scoring_weights is what's actually in effect right now, not a claim —
    proposals are never auto-applied, so this is the honest way to show
    whether one was adopted."""
    _isolate_daily_summary_dirs(monkeypatch, tmp_path)
    weights = {"sentiment": 0.25, "technical": 0.30, "fundamental": 0.15, "catalyst": 0.20, "historical_hitrate": 0.10}
    monkeypatch.setattr(rb, "load_agent_config", lambda: {"scoring_weights": weights})

    summary = rb.build_daily_summary("2026-09-24")

    assert summary["scoring_weights"] == weights


def test_build_daily_summary_no_proposal_without_enough_history(monkeypatch, tmp_path):
    _isolate_daily_summary_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(rb, "load_agent_config", lambda: {"scoring_weights": {}})

    summary = rb.build_daily_summary("2026-09-24")

    assert summary["optimization_proposals"] == ["No performance history yet — nothing to propose."]
    assert summary["signal_hit_rates"] == {}
    assert summary["learning_outcome"] is None


def test_build_daily_summary_surfaces_a_real_proposal_and_outcome_note(monkeypatch, tmp_path):
    _isolate_daily_summary_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(rb, "load_agent_config", lambda: {"scoring_weights": {}})
    (tmp_path / "performance").mkdir(parents=True, exist_ok=True)
    (tmp_path / "performance" / "strategy_metrics.json").write_text(
        json.dumps(
            {
                "by_signal_type": {
                    "sentiment": {"hit_rate": 0.35, "n": 10},
                    "catalyst": {"hit_rate": 0.72, "n": 10},
                }
            }
        )
    )

    summary = rb.build_daily_summary("2026-09-24")

    assert len(summary["optimization_proposals"]) == 1
    assert "sentiment" in summary["optimization_proposals"][0]
    assert "catalyst" in summary["optimization_proposals"][0]
    assert summary["signal_hit_rates"]["sentiment"]["hit_rate"] == 0.35
    assert summary["learning_outcome"] is not None
    assert "sentiment" in summary["learning_outcome"] and "catalyst" in summary["learning_outcome"]
    assert "not a projected return" in summary["learning_outcome"].lower()


def test_build_daily_summary_no_outcome_note_when_gap_too_small(monkeypatch, tmp_path):
    _isolate_daily_summary_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(rb, "load_agent_config", lambda: {"scoring_weights": {}})
    (tmp_path / "performance").mkdir(parents=True, exist_ok=True)
    (tmp_path / "performance" / "strategy_metrics.json").write_text(
        json.dumps({"by_signal_type": {"sentiment": {"hit_rate": 0.60, "n": 10}, "catalyst": {"hit_rate": 0.65, "n": 10}}})
    )

    summary = rb.build_daily_summary("2026-09-24")

    assert summary["learning_outcome"] is None
    assert "No signal-type gap large enough" in summary["optimization_proposals"][0]


# --- _categorize_refusal -------------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("Cannot verify the 5.0% per-position cap (timeout) — refusing to proceed blind.", "data unavailable (failed closed)"),
        ("TSLA is an options contract — this project never trades options.", "options ban"),
        ("TSLA confidence 70 is below the 85% bar required to buy (...)", "below min confidence to buy"),
        ("Daily loss -2.50% has reached the 2.0% cap — halted for the day (...)", "daily loss halt"),
        ("SELL 10 TSLA refused: no existing long position to reduce (held: 0.0).", "short-sale block"),
        ("SELL 10 TSLA refused: only 5 shares held — selling 10 would open a short.", "short-sale block"),
        ("TSLA would reach 6.00% of the 100,000.00 portfolio, over the 5.0% cap.", "position size cap"),
        ("Opening TSLA would exceed the 10-position concurrent-positions cap (...)", "max concurrent positions"),
        ("TSLA would push the Technology sector past the 30.0% sector concentration cap.", "sector concentration cap"),
        ("Daily trade cap reached: 5 of 5 orders already submitted today.", "daily trade count cap"),
        ("TSLA's price has moved 4.00% since this recommendation was scored, past the 3.0% drift limit.", "stale: price drift"),
        ("TSLA's technical signal has flipped since this recommendation was scored.", "stale: technical reversal"),
        ("No approval record for TSLA @ pre_open.", "no approval record"),
        ("Approval for TSLA has expired.", "approval expired"),
        ("Requested qty 10 does not match approved qty 5 within 1.0% tolerance.", "qty mismatch"),
        ("Kill switch is off (config/risk_limits.yaml: trading_enabled=false).", "kill switch off"),
        ("Some entirely unrecognized refusal text.", "other"),
    ],
)
def test_categorize_refusal_matches_known_guardrail_messages(reason, expected):
    assert rb._categorize_refusal(reason) == expected


# --- build_weekly_learning_review ---------------------------------------------


def _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "RECOMMENDATIONS_DIR", tmp_path / "recommendations")
    monkeypatch.setattr(rb, "APPROVALS_DIR", tmp_path / "approvals")
    monkeypatch.setattr(rb, "TRADES_DIR", tmp_path / "trades")
    monkeypatch.setattr(rb, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(rb, "PERFORMANCE_DIR", tmp_path / "performance")
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.PERFORMANCE_DIR", tmp_path / "performance")
    monkeypatch.setattr(rb, "load_agent_config", lambda: {"scoring_weights": {}})
    monkeypatch.setattr(rb, "load_risk_limits", lambda: {"execution": {"max_price_drift_pct": 3.0}})
    # build_weekly_learning_review() also builds and saves optimization
    # options — keep those reads/writes off the real config and data/.
    monkeypatch.setattr("trading_agent.optimizations.OPTIMIZATIONS_DIR", tmp_path / "optimizations")
    monkeypatch.setattr("trading_agent.optimizations.load_agent_config", lambda: {"scoring_weights": {}})
    monkeypatch.setattr(
        "trading_agent.optimizations.load_risk_limits",
        lambda: {"execution": {"max_price_drift_pct": 3.0}, "operational": {"approval_expiry_hours": 2}},
    )
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_positions", lambda: [])
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_account",
        lambda: {"equity": "100000", "cash": "50000", "buying_power": "50000", "last_equity": "100000"},
    )


def _rec(ticker, checkpoint, action, confidence=90, reference_price=100.0):
    return {
        "ticker": ticker,
        "checkpoint": checkpoint,
        "action": action,
        "confidence": confidence,
        "reference_price": reference_price,
    }


def test_build_weekly_learning_review_partitions_unexecuted_recs(monkeypatch, tmp_path):
    """Executed, never-decided, approved-but-not-executed, human-rejected,
    and auto-refused recommendations must each land in exactly one bucket —
    no double counting across them."""
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote",
        lambda ticker: {"bid_price": 100.0, "ask_price": 100.0},
    )

    week_start = "2026-09-21"  # a Monday
    day = week_start

    append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec("EXEC", "pre_open", "BUY"))
    append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec("NEVER", "pre_open", "BUY"))
    append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec("APPROVED", "pre_open", "BUY"))
    append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec("REJECTED", "pre_open", "BUY"))
    append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec("REFUSED", "pre_open", "BUY"))

    append_json(
        day_dir(rb.APPROVALS_DIR, day) / "decisions_pre_open.json",
        {"ticker": "EXEC", "checkpoint": "pre_open", "decision": "approve", "terms": {"qty": 5}},
    )
    append_json(
        day_dir(rb.APPROVALS_DIR, day) / "decisions_pre_open.json",
        {"ticker": "APPROVED", "checkpoint": "pre_open", "decision": "approve", "terms": {"qty": 5}},
    )
    append_json(
        day_dir(rb.APPROVALS_DIR, day) / "decisions_pre_open.json",
        {"ticker": "REJECTED", "checkpoint": "pre_open", "decision": "reject", "terms": {}},
    )

    append_json(
        day_dir(rb.TRADES_DIR, day) / "orders_submitted.json",
        {"order": {"symbol": "EXEC", "side": "buy", "qty": 5}, "recommendation": {"checkpoint": "pre_open", "ticker": "EXEC"}},
    )
    append_json(
        day_dir(rb.TRADES_DIR, day) / "auto_apply_attempts_pre_open.json",
        {"ticker": "REFUSED", "checkpoint": "pre_open", "status": "refused", "reason": "Kill switch is off (...)"},
    )

    review = rb.build_weekly_learning_review(week_start)

    assert review["actionable_count"] == 5
    assert review["executed_count"] == 1
    assert [r["ticker"] for r in review["never_decided"]] == ["NEVER"]
    assert [r["ticker"] for r in review["approved_not_executed"]] == ["APPROVED"]
    assert [r["ticker"] for r in review["rejected"]] == ["REJECTED"]
    assert review["auto_apply_attempts_by_status"] == {"submitted": 0, "refused": 1, "skipped": 0, "error": 0}
    assert review["refusal_breakdown"] == {"kill switch off": 1}


def test_build_weekly_learning_review_missed_opportunity_counterfactual(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote",
        lambda ticker: {"bid_price": 110.0, "ask_price": 110.0} if ticker == "WON" else {"bid_price": 90.0, "ask_price": 90.0},
    )

    week_start = "2026-09-21"
    day = week_start
    append_json(
        day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json",
        _rec("WON", "pre_open", "BUY", reference_price=100.0),
    )
    append_json(
        day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json",
        _rec("LOST", "pre_open", "BUY", reference_price=100.0),
    )

    review = rb.build_weekly_learning_review(week_start)

    by_ticker = {m["ticker"]: m for m in review["missed_opportunities"]}
    assert by_ticker["WON"]["would_have_helped"] is True
    assert by_ticker["WON"]["pct_change"] == 10.0
    assert by_ticker["LOST"]["would_have_helped"] is False
    assert by_ticker["LOST"]["pct_change"] == -10.0
    assert by_ticker["WON"]["category"] == "never decided"


def test_build_weekly_learning_review_excludes_candidates_with_no_reference_price(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "trading_agent.data.alpaca_client.get_latest_quote",
        lambda ticker: {"bid_price": 100.0, "ask_price": 100.0},
    )

    day = "2026-09-21"
    append_json(
        day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json",
        _rec("NOPRICE", "pre_open", "BUY", reference_price=None),
    )

    review = rb.build_weekly_learning_review(day)

    assert review["missed_opportunities"] == []


def test_build_weekly_learning_review_portfolio_status_live_snapshot(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    review = rb.build_weekly_learning_review("2026-09-21")

    assert review["portfolio_status"] == {"equity": 100000.0, "cash": 50000.0, "buying_power": 50000.0, "error": None}


def test_build_weekly_learning_review_portfolio_status_unavailable(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    def boom():
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", boom)

    review = rb.build_weekly_learning_review("2026-09-21")

    assert review["portfolio_status"]["equity"] is None
    assert "alpaca unreachable" in review["portfolio_status"]["error"]


def test_build_weekly_learning_review_writes_markdown_report(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    review = rb.build_weekly_learning_review("2026-09-21")

    path = Path(review["report_path"])
    assert path.exists()
    assert path.parent.name == "learning"
    text = path.read_text()
    assert "# Weekly Learning Review" in text
    assert "## Orders not placed over the lookback window" in text
    assert "## Opportunities lost or avoided" in text
    assert "never applied automatically" in text or "never applied" in text


def test_build_weekly_learning_review_config_tuning_note_for_stale_refusals(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    day = "2026-09-21"
    for i in range(3):
        ticker = f"STALE{i}"
        append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec(ticker, "pre_open", "BUY"))
        append_json(
            day_dir(rb.TRADES_DIR, day) / "auto_apply_attempts_pre_open.json",
            {
                "ticker": ticker,
                "checkpoint": "pre_open",
                "status": "refused",
                "reason": f"{ticker}'s price has moved 5.00% since this recommendation was scored, past the 3.0% drift limit.",
            },
        )

    review = rb.build_weekly_learning_review(day)

    notes = " ".join(review["config_tuning_notes"])
    assert "stale-recommendation blocks" in notes
    assert "3.0%" in notes


def test_build_weekly_learning_review_no_proposal_without_history(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    review = rb.build_weekly_learning_review("2026-09-21")

    assert review["optimization_proposals"] == ["No performance history yet — nothing to propose."]
    assert review["signal_hit_rates"] == {}
    assert review["learning_outcome"] is None


# --- rolling lookback window (per user instruction 2026-10-02) ---------------


def test_lookback_dates_returns_n_days_ending_inclusive():
    dates = rb._lookback_dates("2026-10-02", 30)
    assert len(dates) == 30
    assert dates[0] == "2026-09-03"
    assert dates[-1] == "2026-10-02"


def test_lookback_dates_defaults_to_today(monkeypatch):
    class _FixedDatetime(rb.datetime):
        @classmethod
        def now(cls, tz=None):
            return rb.datetime(2026, 10, 2, tzinfo=tz)

    monkeypatch.setattr(rb, "datetime", _FixedDatetime)
    dates = rb._lookback_dates(None, 5)
    assert dates == ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]


def test_build_weekly_learning_review_defaults_to_30_day_window(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    review = rb.build_weekly_learning_review("2026-10-02")

    assert review["lookback_days"] == 30
    assert review["window_start"] == "2026-09-03"
    assert review["window_end"] == "2026-10-02"


def test_build_weekly_learning_review_honors_config_lookback_override(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})
    monkeypatch.setattr(rb, "load_agent_config", lambda: {"scoring_weights": {}, "reporting": {"weekly_learning_review_lookback_days": 14}})

    review = rb.build_weekly_learning_review("2026-10-02")

    assert review["lookback_days"] == 14
    assert review["window_start"] == "2026-09-19"


def test_build_weekly_learning_review_explicit_lookback_days_overrides_config(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})
    monkeypatch.setattr(rb, "load_agent_config", lambda: {"scoring_weights": {}, "reporting": {"weekly_learning_review_lookback_days": 14}})

    review = rb.build_weekly_learning_review("2026-10-02", lookback_days=5)

    assert review["lookback_days"] == 5
    assert review["window_start"] == "2026-09-28"


def test_build_weekly_learning_review_picks_up_recs_from_anywhere_in_the_30_day_window(monkeypatch, tmp_path):
    """A rec from 3 weeks before `as_of` must still be picked up — the whole
    point of broadening from a 7-day week to a 30-day rolling window."""
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    old_day = "2026-09-10"  # 21 days before as_of, inside a 30-day window but outside any 7-day week containing it
    append_json(day_dir(rb.RECOMMENDATIONS_DIR, old_day) / "recs_pre_open.json", _rec("OLD", "pre_open", "BUY"))

    review = rb.build_weekly_learning_review("2026-10-01")

    assert review["actionable_count"] == 1
    assert [r["ticker"] for r in review["never_decided"]] == ["OLD"]


# --- accuracy fixes found by the first live run (2026-10-02) -----------------


def test_auto_approval_with_no_trade_or_attempt_is_not_blamed_on_a_human(monkeypatch, tmp_path):
    """auto_apply() writes its approval before submitting, so an auto approval
    from before the attempt log existed shows up with no trade and no attempt.
    That's an unlogged auto-apply outcome, not a human who forgot to execute."""
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    day = "2026-09-25"
    append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec("AUTO", "pre_open", "BUY"))
    append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec("HUMAN", "pre_open", "BUY"))
    append_json(
        day_dir(rb.APPROVALS_DIR, day) / "decisions_pre_open.json",
        {"ticker": "AUTO", "checkpoint": "pre_open", "decision": "approve", "terms": {"source": "auto", "qty": 5}},
    )
    append_json(
        day_dir(rb.APPROVALS_DIR, day) / "decisions_pre_open.json",
        {"ticker": "HUMAN", "checkpoint": "pre_open", "decision": "approve", "terms": {"qty": 5}},
    )

    review = rb.build_weekly_learning_review("2026-10-02")

    assert [r["ticker"] for r in review["auto_outcome_unlogged"]] == ["AUTO"]
    assert [r["ticker"] for r in review["approved_not_executed"]] == ["HUMAN"]
    notes = " ".join(review["config_tuning_notes"])
    assert "predate the per-attempt log" in notes


def test_rerun_checkpoint_duplicates_are_counted_once(monkeypatch, tmp_path):
    """A checkpoint re-run the same day appends the same recommendation again;
    it must not inflate the actionable or executed counts."""
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    day = "2026-10-01"
    for _ in range(2):
        append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec("DUP", "pre_open", "BUY"))
    append_json(
        day_dir(rb.TRADES_DIR, day) / "orders_submitted.json",
        {"order": {"symbol": "DUP", "side": "buy", "qty": 5}, "recommendation": {"checkpoint": "pre_open", "ticker": "DUP"}},
    )

    review = rb.build_weekly_learning_review("2026-10-02")

    assert review["actionable_count"] == 1
    assert review["executed_count"] == 1
    assert review["executed_count"] <= review["trades_count"]


def test_attempt_log_coverage_is_reported_not_assumed_to_be_the_whole_window(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})

    day = "2026-09-30"
    append_json(day_dir(rb.RECOMMENDATIONS_DIR, day) / "recs_pre_open.json", _rec("REF", "pre_open", "BUY"))
    append_json(
        day_dir(rb.TRADES_DIR, day) / "auto_apply_attempts_pre_open.json",
        {
            "ticker": "REF",
            "checkpoint": "pre_open",
            "status": "refused",
            "reason": "REF's price has moved 5.00% since this recommendation was scored, past the 3.0% drift limit.",
        },
    )

    review = rb.build_weekly_learning_review("2026-10-02")

    assert review["attempt_log_days"] == ["2026-09-30"]
    # Logging covers 09-30 through the window's end (10-02), not just the one
    # day that happened to have a log file.
    assert review["attempt_log_coverage_days"] == 3
    assert review["attempt_log_span"] == "3 of the last 30 days, since 2026-09-30"
    text = Path(review["report_path"]).read_text()
    assert "Refusal breakdown (3 of the last 30 days, since 2026-09-30):" in text
    assert "logs cover 3 of the last 30 days, since 2026-09-30" in " ".join(
        review["config_tuning_notes"]
    )


def test_attempt_log_span_when_no_logs_or_full_coverage():
    window = rb._lookback_dates("2026-10-02", 30)
    assert rb._attempt_log_span([], window) == "no attempt logs in the last 30 days"
    assert rb._attempt_log_span([window[0]], window) == "all 30 days"


def test_weight_proposal_needs_enough_outcomes_per_signal(monkeypatch, tmp_path):
    """3 outcomes per signal (the first live run's actual history) is too few
    to compare signals — no proposal and no outcome note, rather than a
    'technical 100% vs sentiment 67%' finding built on noise."""
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})
    (tmp_path / "performance").mkdir(parents=True, exist_ok=True)
    (tmp_path / "performance" / "strategy_metrics.json").write_text(
        json.dumps(
            {
                "by_signal_type": {
                    "sentiment": {"hit_rate": 0.67, "n": 3},
                    "technical": {"hit_rate": 1.0, "n": 3},
                }
            }
        )
    )

    review = rb.build_weekly_learning_review("2026-10-02")

    assert len(review["optimization_proposals"]) == 1
    assert "Not enough outcome history" in review["optimization_proposals"][0]
    assert "3 outcome(s)" in review["optimization_proposals"][0]
    assert review["learning_outcome"] is None


def test_weight_proposal_ignores_under_sampled_signals_but_compares_the_rest(monkeypatch, tmp_path):
    _isolate_weekly_learning_review_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_latest_quote", lambda ticker: {})
    (tmp_path / "performance").mkdir(parents=True, exist_ok=True)
    (tmp_path / "performance" / "strategy_metrics.json").write_text(
        json.dumps(
            {
                "by_signal_type": {
                    "sentiment": {"hit_rate": 0.35, "n": 12},
                    "catalyst": {"hit_rate": 0.75, "n": 12},
                    "technical": {"hit_rate": 0.0, "n": 2},  # worst, but too few outcomes to count
                }
            }
        )
    )

    review = rb.build_weekly_learning_review("2026-10-02")

    proposal = review["optimization_proposals"][0]
    assert "'sentiment'" in proposal and "'catalyst'" in proposal
    assert "technical" not in proposal

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
    assert "## Orders not placed this week" in text
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

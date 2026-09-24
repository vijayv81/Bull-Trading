import pytest

from trading_agent.reporting import report_builder as rb


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

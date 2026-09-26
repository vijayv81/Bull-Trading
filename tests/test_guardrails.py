"""Safety-critical: the four portfolio guardrails (5% per position, 2% daily
loss halt, no options ever, no short positions ever) must hold, and must fail
closed when account state can't be read.
"""

import pytest

from trading_agent import guardrails as g


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(
        g,
        "load_risk_limits",
        lambda: {
            "position": {"max_position_pct_of_portfolio": 5.0},
            "portfolio": {"max_daily_drawdown_pct": 2.0},
        },
    )


@pytest.fixture
def concurrent_cap(monkeypatch):
    monkeypatch.setattr(
        g,
        "load_risk_limits",
        lambda: {"position": {"max_concurrent_positions": 2}},
    )


def _account(monkeypatch, equity, last_equity=100_000.0):
    monkeypatch.setattr(
        g, "_account", lambda: {"equity": equity, "last_equity": last_equity}
    )


# --- no options, ever -------------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    ["AAPL240119C00150000", "TSLA260918P00420000", "F240119C00012500", "spy240119c00470000"],
)
def test_option_symbols_detected(symbol):
    assert g.is_option_symbol(symbol) is True


@pytest.mark.parametrize("symbol", ["AAPL", "TSLA", "BRK", "GOOG", "F", "NVDA"])
def test_equity_symbols_not_flagged_as_options(symbol):
    assert g.is_option_symbol(symbol) is False
    assert g.options_reason(symbol) is None


def test_options_reason_explains_the_ban():
    reason = g.options_reason("AAPL240119C00150000")
    assert "never trades options" in reason


# --- daily loss cap ---------------------------------------------------------


def test_daily_loss_at_cap_halts(limits, monkeypatch):
    _account(monkeypatch, equity=98_000.0)  # -2.00%
    assert "halted for the day" in g.daily_loss_reason()


def test_daily_loss_beyond_cap_halts(limits, monkeypatch):
    _account(monkeypatch, equity=95_000.0)  # -5.00%
    assert "halted for the day" in g.daily_loss_reason()


def test_daily_loss_within_cap_passes(limits, monkeypatch):
    _account(monkeypatch, equity=98_500.0)  # -1.50%
    assert g.daily_loss_reason() is None


def test_daily_gain_passes(limits, monkeypatch):
    _account(monkeypatch, equity=103_000.0)
    assert g.daily_loss_reason() is None


def test_daily_loss_fails_closed_when_account_unreadable(limits, monkeypatch):
    def boom():
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr(g, "_account", boom)
    assert "refusing to proceed blind" in g.daily_loss_reason()


def test_daily_loss_fails_closed_on_zero_prior_equity(limits, monkeypatch):
    _account(monkeypatch, equity=100.0, last_equity=0.0)
    assert "Cannot verify" in g.daily_loss_reason()


# --- max 5% per position ----------------------------------------------------


@pytest.fixture
def flat_book(monkeypatch):
    monkeypatch.setattr(g, "_positions", lambda: [])
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)


def test_buy_within_cap_passes(limits, flat_book, monkeypatch):
    _account(monkeypatch, equity=100_000.0)
    assert g.position_size_reason("TSLA", "BUY", qty=49) is None  # 4.9%


def test_buy_over_cap_refuses(limits, flat_book, monkeypatch):
    _account(monkeypatch, equity=100_000.0)
    assert "over the 5.0% cap" in g.position_size_reason("TSLA", "BUY", qty=51)  # 5.1%


def test_existing_position_counts_toward_cap(limits, monkeypatch):
    _account(monkeypatch, equity=100_000.0)
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    monkeypatch.setattr(
        g, "_positions", lambda: [{"symbol": "TSLA", "market_value": "4000"}]
    )
    # 4% already held + 2% more = 6%, over the cap even though each buy is small.
    assert "over the 5.0% cap" in g.position_size_reason("TSLA", "BUY", qty=20)


def test_other_tickers_do_not_count_toward_cap(limits, monkeypatch):
    _account(monkeypatch, equity=100_000.0)
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    monkeypatch.setattr(
        g, "_positions", lambda: [{"symbol": "GOOG", "market_value": "4000"}]
    )
    assert g.position_size_reason("TSLA", "BUY", qty=20) is None


def test_sell_is_never_blocked_by_size_cap(limits, flat_book, monkeypatch):
    _account(monkeypatch, equity=100_000.0)
    assert g.position_size_reason("TSLA", "SELL", qty=10_000) is None


def test_position_size_fails_closed_when_account_unreadable(limits, flat_book, monkeypatch):
    def boom():
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr(g, "_account", boom)
    assert "refusing to proceed blind" in g.position_size_reason("TSLA", "BUY", qty=1)


def test_position_size_fails_closed_on_zero_equity(limits, flat_book, monkeypatch):
    _account(monkeypatch, equity=0.0)
    assert "Cannot verify" in g.position_size_reason("TSLA", "BUY", qty=1)


# --- max daily trade count (all sources combined) ------------------------------


@pytest.fixture
def daily_trade_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"portfolio": {"max_daily_trades": 5}})
    monkeypatch.setattr(g, "TRADES_DIR", tmp_path)
    return tmp_path


def _write_trades(tmp_path, n, day=None):
    import json

    from trading_agent.utils import today

    day_path = tmp_path / (day or today())
    day_path.mkdir(parents=True, exist_ok=True)
    (day_path / "orders_submitted.json").write_text(
        json.dumps([{"order": {"symbol": "X"}, "source": "human"} for _ in range(n)])
    )


def test_under_daily_trade_cap_passes(daily_trade_cap):
    _write_trades(daily_trade_cap, 3)
    assert g.daily_trade_count_reason() is None


def test_at_daily_trade_cap_refuses(daily_trade_cap):
    _write_trades(daily_trade_cap, 5)
    reason = g.daily_trade_count_reason()
    assert "5 of 5 orders already submitted today" in reason


def test_over_daily_trade_cap_refuses(daily_trade_cap):
    _write_trades(daily_trade_cap, 7)
    assert "Daily trade cap reached" in g.daily_trade_count_reason()


def test_no_trades_yet_today_passes(daily_trade_cap):
    assert g.daily_trade_count_reason() is None


def test_no_daily_trade_cap_configured_never_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"portfolio": {}})
    monkeypatch.setattr(g, "TRADES_DIR", tmp_path)
    _write_trades(tmp_path, 100)
    assert g.daily_trade_count_reason() is None


def test_daily_trade_count_fails_closed_on_unreadable_file(daily_trade_cap):
    from trading_agent.utils import today

    day_path = daily_trade_cap / today()
    day_path.mkdir(parents=True, exist_ok=True)
    (day_path / "orders_submitted.json").write_text("{not valid json")
    assert "refusing to proceed blind" in g.daily_trade_count_reason()


# --- max concurrent positions -------------------------------------------------


def test_new_symbol_under_cap_passes(concurrent_cap, monkeypatch):
    monkeypatch.setattr(g, "_positions", lambda: [{"symbol": "GOOG"}])
    assert g.position_count_reason("TSLA", "BUY") is None


def test_new_symbol_at_cap_refuses(concurrent_cap, monkeypatch):
    monkeypatch.setattr(g, "_positions", lambda: [{"symbol": "GOOG"}, {"symbol": "AAPL"}])
    reason = g.position_count_reason("TSLA", "BUY")
    assert "2-position concurrent-positions cap" in reason


def test_adding_to_existing_symbol_not_counted_as_new(concurrent_cap, monkeypatch):
    monkeypatch.setattr(g, "_positions", lambda: [{"symbol": "GOOG"}, {"symbol": "AAPL"}])
    # Already held — this is position_size_reason()'s job to cap, not this check's.
    assert g.position_count_reason("AAPL", "BUY") is None


def test_sell_never_checked_for_concurrent_cap(concurrent_cap, monkeypatch):
    monkeypatch.setattr(g, "_positions", lambda: [{"symbol": "GOOG"}, {"symbol": "AAPL"}])
    assert g.position_count_reason("TSLA", "SELL") is None


def test_no_cap_configured_never_refuses(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"position": {}})
    monkeypatch.setattr(g, "_positions", lambda: [{"symbol": s} for s in "ABCDEFGHIJ"])
    assert g.position_count_reason("TSLA", "BUY") is None


def test_position_count_fails_closed_when_positions_unreadable(concurrent_cap, monkeypatch):
    def boom():
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr(g, "_positions", boom)
    assert "refusing to proceed blind" in g.position_count_reason("TSLA", "BUY")


# --- sector concentration -------------------------------------------------------


@pytest.fixture
def sector_cap(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"portfolio": {"max_sector_concentration_pct": 25.0}})


def _sectors(mapping, monkeypatch):
    monkeypatch.setattr("trading_agent.data.market_data.get_sector", lambda symbol: mapping.get(symbol))


def test_sector_reason_none_when_no_cap_configured(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"portfolio": {}})
    assert g.sector_concentration_reason("TSLA", "BUY", qty=1) is None


def test_sector_reason_sell_never_checked(sector_cap):
    assert g.sector_concentration_reason("TSLA", "SELL", qty=1_000_000) is None


def test_sector_reason_skips_when_sector_unknown(sector_cap, monkeypatch):
    _sectors({"TSLA": None}, monkeypatch)

    def fail():
        raise AssertionError("must not read account state when the sector itself is unknown")

    monkeypatch.setattr(g, "_account", fail)
    assert g.sector_concentration_reason("TSLA", "BUY", qty=1) is None


def test_sector_reason_within_cap_passes(sector_cap, monkeypatch):
    _sectors({"TSLA": "Automotive"}, monkeypatch)
    monkeypatch.setattr(g, "_account", lambda: {"equity": "100000"})
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    monkeypatch.setattr(g, "_positions", lambda: [])
    assert g.sector_concentration_reason("TSLA", "BUY", qty=200) is None  # $20,000 = 20%


def test_sector_reason_over_cap_refuses(sector_cap, monkeypatch):
    _sectors({"TSLA": "Automotive"}, monkeypatch)
    monkeypatch.setattr(g, "_account", lambda: {"equity": "100000"})
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    monkeypatch.setattr(g, "_positions", lambda: [])
    reason = g.sector_concentration_reason("TSLA", "BUY", qty=300)  # $30,000 = 30%
    assert "Automotive" in reason
    assert "over the 25.0% cap" in reason


def test_sector_reason_other_symbols_same_sector_count_toward_cap(sector_cap, monkeypatch):
    _sectors({"TSLA": "Automotive", "F": "Automotive", "GOOG": "Technology"}, monkeypatch)
    monkeypatch.setattr(g, "_account", lambda: {"equity": "100000"})
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    monkeypatch.setattr(
        g,
        "_positions",
        lambda: [
            {"symbol": "F", "market_value": "20000"},  # same sector, counts
            {"symbol": "GOOG", "market_value": "50000"},  # different sector, doesn't count
        ],
    )
    # 20,000 (F, same sector) + 10,000 (this buy) = 30,000 = 30%, over the 25% cap.
    reason = g.sector_concentration_reason("TSLA", "BUY", qty=100)
    assert "over the 25.0% cap" in reason


def test_sector_reason_existing_same_symbol_not_double_counted(sector_cap, monkeypatch):
    _sectors({"TSLA": "Automotive"}, monkeypatch)
    monkeypatch.setattr(g, "_account", lambda: {"equity": "100000"})
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    monkeypatch.setattr(g, "_positions", lambda: [{"symbol": "TSLA", "market_value": "10000"}])
    # existing 10,000 + this buy's 10,000 = 20,000 = 20%, within the 25% cap —
    # TSLA's own existing position must not also be summed into "other sector value".
    assert g.sector_concentration_reason("TSLA", "BUY", qty=100) is None


def test_sector_reason_fails_closed_when_account_unreadable(sector_cap, monkeypatch):
    _sectors({"TSLA": "Automotive"}, monkeypatch)

    def boom():
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr(g, "_account", boom)
    reason = g.sector_concentration_reason("TSLA", "BUY", qty=1)
    assert "refusing to proceed blind" in reason


# --- no short positions, ever ------------------------------------------------


def test_buy_side_never_checked_for_short_sale(monkeypatch):
    monkeypatch.setattr(g, "_positions", lambda: [])
    assert g.short_sale_reason("TSLA", "BUY", qty=1_000_000) is None


def test_sell_with_no_existing_position_refuses(monkeypatch):
    monkeypatch.setattr(g, "_positions", lambda: [])
    reason = g.short_sale_reason("TSLA", "SELL", qty=10)
    assert "never opens short positions" in reason


def test_sell_exceeding_held_qty_refuses(monkeypatch):
    monkeypatch.setattr(g, "_positions", lambda: [{"symbol": "TSLA", "qty": "5"}])
    reason = g.short_sale_reason("TSLA", "SELL", qty=10)
    assert "never opens short positions" in reason


def test_sell_within_held_qty_passes(monkeypatch):
    monkeypatch.setattr(g, "_positions", lambda: [{"symbol": "TSLA", "qty": "10"}])
    assert g.short_sale_reason("TSLA", "SELL", qty=10) is None
    assert g.short_sale_reason("TSLA", "SELL", qty=5) is None


def test_short_sale_fails_closed_when_positions_unreadable(monkeypatch):
    def boom():
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr(g, "_positions", boom)
    assert "refusing to proceed blind" in g.short_sale_reason("TSLA", "SELL", qty=10)


# --- stale recommendation (re-verified right before order submission) ---------


@pytest.fixture
def execution_cfg(monkeypatch):
    monkeypatch.setattr(
        g,
        "load_risk_limits",
        lambda: {"execution": {"max_price_drift_pct": 3.0, "reverify_technical": True}},
    )


def _rec(action="BUY", reference_price=100.0):
    return {"ticker": "TSLA", "action": action, "reference_price": reference_price}


def _bars(n, close=100.0):
    return [{"close": close} for _ in range(n)]


def test_stale_reason_none_for_hold(execution_cfg):
    assert g.stale_recommendation_reason({"ticker": "TSLA", "action": "HOLD"}) is None


def test_stale_reason_price_within_drift_passes(execution_cfg, monkeypatch):
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 101.0)  # 1% drift
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars", lambda symbol: _bars(60, close=101.0))
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.technical_score", lambda bars: 0.8)
    assert g.stale_recommendation_reason(_rec(action="BUY")) is None


def test_stale_reason_price_beyond_drift_refuses(execution_cfg, monkeypatch):
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 110.0)  # 10% drift
    reason = g.stale_recommendation_reason(_rec(action="BUY"))
    assert "price has moved" in reason
    assert "10.00%" in reason


def test_stale_reason_skips_price_check_without_reference_price(execution_cfg, monkeypatch):
    def fail(symbol):
        raise AssertionError("must not check price without a reference_price to compare against")

    monkeypatch.setattr(g, "_reference_price", fail)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars", lambda symbol: _bars(60))
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.technical_score", lambda bars: 0.8)
    assert g.stale_recommendation_reason(_rec(action="BUY", reference_price=None)) is None


def test_stale_reason_skips_price_check_when_drift_cap_unset(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"execution": {"reverify_technical": False}})

    def fail(symbol):
        raise AssertionError("must not check price when max_price_drift_pct is unset")

    monkeypatch.setattr(g, "_reference_price", fail)
    assert g.stale_recommendation_reason(_rec(action="BUY")) is None


def test_stale_reason_technical_flip_refuses(execution_cfg, monkeypatch):
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)  # no drift
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars", lambda symbol: _bars(60))
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.technical_score", lambda bars: 0.3)  # -> SELL
    reason = g.stale_recommendation_reason(_rec(action="BUY"))
    assert "technical signal has flipped" in reason
    assert "BUY then, SELL now" in reason


def test_stale_reason_technical_agrees_passes(execution_cfg, monkeypatch):
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars", lambda symbol: _bars(60))
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.technical_score", lambda bars: 0.9)
    assert g.stale_recommendation_reason(_rec(action="BUY")) is None


def test_stale_reason_insufficient_bars_skips_technical_recheck(execution_cfg, monkeypatch):
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars", lambda symbol: _bars(10))  # too few
    monkeypatch.setattr(
        "trading_agent.scoring.recommendation_engine.technical_score",
        lambda bars: (_ for _ in ()).throw(AssertionError("must not be called without enough bars")),
    )
    assert g.stale_recommendation_reason(_rec(action="BUY")) is None


def test_stale_reason_reverify_technical_false_skips_direction_check(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"execution": {"reverify_technical": False}})
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    assert g.stale_recommendation_reason(_rec(action="BUY")) is None


def test_stale_reason_fails_closed_on_unreadable_quote(execution_cfg, monkeypatch):
    def boom(symbol):
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr(g, "_reference_price", boom)
    reason = g.stale_recommendation_reason(_rec(action="BUY"))
    assert "refusing to proceed blind" in reason


def test_stale_reason_fails_closed_on_unreadable_bars(execution_cfg, monkeypatch):
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)

    def boom(symbol):
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars", boom)
    reason = g.stale_recommendation_reason(_rec(action="BUY"))
    assert "refusing to proceed blind" in reason


def test_stale_reason_take_profit_direction_biased_sell_still_reverified(execution_cfg, monkeypatch):
    """A stop-loss/take-profit-biased SELL (see recommendation_engine's
    sell_pressure) is re-verified the same as a pure-technical one — the
    action recorded on the rec is what's compared, regardless of why it
    became SELL."""
    monkeypatch.setattr(g, "_reference_price", lambda symbol: 100.0)
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_recent_bars", lambda symbol: _bars(60))
    monkeypatch.setattr("trading_agent.scoring.recommendation_engine.technical_score", lambda bars: 0.9)  # -> BUY now
    reason = g.stale_recommendation_reason(_rec(action="SELL"))
    assert "technical signal has flipped" in reason
    assert "SELL then, BUY now" in reason

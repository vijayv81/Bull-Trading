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

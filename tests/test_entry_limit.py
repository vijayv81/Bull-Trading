"""guardrails.entry_limit_price(): limit pricing + spread check (2026-10-09 FVNNU)."""

import pytest

from trading_agent import guardrails as g


def _cfg(**execution):
    return lambda: {"execution": execution}


def _quote(monkeypatch, bid, ask):
    from trading_agent.data import alpaca_client

    monkeypatch.setattr(alpaca_client, "get_latest_quote", lambda t: {"bid_price": bid, "ask_price": ask})


def test_buy_limit_is_ask_plus_slippage(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", _cfg(order_type="limit", limit_slippage_pct=1.0, max_spread_pct=5.0))
    _quote(monkeypatch, 16.0, 16.2)
    assert g.entry_limit_price("FVNNU", "BUY") == (None, 16.36)


def test_wide_spread_refuses_buy(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", _cfg(order_type="limit", max_spread_pct=5.0))
    _quote(monkeypatch, 10.0, 16.0)
    reason, price = g.entry_limit_price("FVNNU", "BUY")
    assert price is None and "spread" in reason


@pytest.mark.parametrize("bid,ask", [(0, 16.0), (16.0, 0)])
def test_one_sided_quote_refuses_buy(monkeypatch, bid, ask):
    monkeypatch.setattr(g, "load_risk_limits", _cfg(order_type="limit"))
    _quote(monkeypatch, bid, ask)
    reason, price = g.entry_limit_price("FVNNU", "BUY")
    assert price is None and "one-sided" in reason


def test_unreadable_quote_fails_closed(monkeypatch):
    from trading_agent.data import alpaca_client

    def boom(t):
        raise RuntimeError("down")

    monkeypatch.setattr(g, "load_risk_limits", _cfg(order_type="limit"))
    monkeypatch.setattr(alpaca_client, "get_latest_quote", boom)
    reason, price = g.entry_limit_price("X", "BUY")
    assert price is None and "refusing" in reason


def test_market_mode_and_default_sells_stay_market(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", _cfg(order_type="market"))
    assert g.entry_limit_price("X", "BUY") == (None, None)
    monkeypatch.setattr(g, "load_risk_limits", _cfg(order_type="limit"))
    assert g.entry_limit_price("X", "SELL") == (None, None)


def test_sell_limit_when_enabled(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", _cfg(order_type="limit", limit_orders_for_sells=True, limit_slippage_pct=2.0))
    _quote(monkeypatch, 10.0, 10.1)
    assert g.entry_limit_price("X", "SELL") == (None, 9.8)


def test_position_cap_uses_the_limit_price(monkeypatch):
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"position": {"max_position_pct_of_portfolio": 5.0}})
    monkeypatch.setattr(g, "_account", lambda: {"equity": "10000"})
    monkeypatch.setattr(g, "_existing_position_value", lambda s: 0.0)
    monkeypatch.setattr(g, "_reference_price", lambda s: 1.0)
    assert g.position_size_reason("X", "BUY", 100, price=1.0) is None
    assert "cap" in g.position_size_reason("X", "BUY", 100, price=10.0)

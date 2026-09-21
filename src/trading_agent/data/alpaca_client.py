"""Alpaca paper-trading client: market data, account/positions, order submission.

Credentials come from APCA_API_KEY_ID / APCA_API_SECRET_KEY via config.require_env
— never from a file. Trading is hard-locked to Alpaca's paper endpoint: see
trading_client() and risk_limits.yaml -> operational.allow_live_trading (plan §9,
API-key-scope guardrail).

submit_market_order() has no knowledge of approvals — it is the low-level Alpaca
call. Callers MUST go through execute/order_manager.py, which enforces the kill
switch and the approval-record match before ever reaching this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from trading_agent.config import load_risk_limits, require_env


def _credentials() -> tuple[str, str]:
    return require_env("APCA_API_KEY_ID"), require_env("APCA_API_SECRET_KEY")


def trading_client() -> TradingClient:
    risk = load_risk_limits().get("operational", {})
    if risk.get("allow_live_trading"):
        raise RuntimeError(
            "allow_live_trading is set in config/risk_limits.yaml — refusing to "
            "construct a trading client automatically. That flag exists to force "
            "a deliberate, reviewed code change (not a config flip) before this "
            "project ever talks to a live trading endpoint."
        )
    key_id, secret_key = _credentials()
    return TradingClient(api_key=key_id, secret_key=secret_key, paper=True)


def data_client() -> StockHistoricalDataClient:
    key_id, secret_key = _credentials()
    return StockHistoricalDataClient(api_key=key_id, secret_key=secret_key)


def get_account() -> dict[str, Any]:
    return trading_client().get_account().model_dump(mode="json")


def get_positions() -> list[dict[str, Any]]:
    return [p.model_dump(mode="json") for p in trading_client().get_all_positions()]


def get_latest_quote(ticker: str) -> dict[str, Any]:
    request = StockLatestQuoteRequest(symbol_or_symbols=ticker)
    quotes = data_client().get_stock_latest_quote(request)
    return quotes[ticker].model_dump(mode="json")


def get_recent_bars(ticker: str, lookback_days: int = 60) -> list[dict[str, Any]]:
    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=lookback_days),
    )
    bars = data_client().get_stock_bars(request)
    symbol_bars = bars[ticker] if ticker in bars.data else []
    return [bar.model_dump(mode="json") for bar in symbol_bars]


def get_market_movers(top_n: int = 20) -> dict[str, Any]:
    """Top gainers/losers by % move, if your Alpaca plan exposes the screener
    endpoint. Never raises — falls back to an empty result so a checkpoint run
    can still proceed off the configured watchlist alone (plan §12: no secondary
    screening vendor is wired up yet).
    """
    try:
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MarketMoversRequest

        key_id, secret_key = _credentials()
        client = ScreenerClient(api_key=key_id, secret_key=secret_key)
        movers = client.get_market_movers(MarketMoversRequest(top=top_n))
        return {
            "gainers": [m.model_dump(mode="json") for m in movers.gainers],
            "losers": [m.model_dump(mode="json") for m in movers.losers],
        }
    except Exception as exc:  # screener may be unavailable on some plans/SDK versions
        return {"gainers": [], "losers": [], "error": str(exc)}


def submit_market_order(ticker: str, side: str, qty: float) -> dict[str, Any]:
    """Submit a paper market order. No approval check here — see module docstring."""
    order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
    request = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    return trading_client().submit_order(request).model_dump(mode="json")

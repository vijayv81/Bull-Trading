"""Core holding: most of the account in a broad index fund, the active strategy
on a small, hard-capped slice.

Per user instruction 2026-10-12. The active strategy has not shown an edge:
technical 40% right and BUYs 22% right live, and a 3-year backtest of the real
scorer trailed buy-and-hold on 10 of 11 liquid names. With the account at 91%
cash after the week of 2026-10-05, none of it was even earning the index. So the
core (`risk_limits.yaml -> core`, SPY by default) is topped up toward a target
share of the portfolio, and everything else is the "satellite", capped by
guardrails.satellite_exposure_reason().

How it stays inside the project's rules:
- It is an ordinary order through execute.order_manager.submit_approved_order(),
  with a decision on disk tagged `source: "core"` (the same shape auto-apply
  writes). Every guardrail runs: kill switch, daily-loss halt (a BUY is refused
  while halted, so the core waits), position cap (the core symbol's own),
  daily order count, limit price and spread check, stale-price check.
- DAY limit order at the ask + the usual slippage, one order a day at one
  checkpoint, at most `max_order_pct_of_portfolio` a time, so it is built over
  days rather than in one print.
- BUY only. It never sells the core and has no stop-loss: it is the index.
  Trimming an overweight core is left to a human.
- The core symbol is never researched, scored or sold by the satellite pass
  (orchestrator.run_checkpoint() leaves it out).
- Never raises into run_checkpoint(); a failure is that attempt's own result.
"""

from __future__ import annotations

from typing import Any

from trading_agent.execute.auto_pilot import _persist_attempt
from trading_agent.execute.order_manager import OrderRefused, submit_approved_order
from trading_agent.guardrails import core_cfg, daily_loss_reason
from trading_agent.notify.approval_gateway import record_decision

CORE_CHECKPOINT = "core"  # the decision/approval bucket, distinct from the 4 scheduled checkpoints


def core_gap_pct(equity: float, core_value: float, cfg: dict[str, Any]) -> float:
    """Points the core is under its target share of the portfolio (negative if over)."""
    if equity <= 0:
        return 0.0
    return float(cfg["target_pct_of_portfolio"]) - core_value / equity * 100


def core_order_qty(equity: float, cash: float, gap_pct: float, price: float, cfg: dict[str, Any]) -> int:
    """Whole shares to buy: the gap, capped at max_order_pct_of_portfolio and at
    the cash on hand. Rounds down."""
    if price <= 0 or equity <= 0:
        return 0
    dollars = min(gap_pct, float(cfg.get("max_order_pct_of_portfolio", gap_pct))) / 100 * equity
    dollars = min(dollars, max(cash, 0.0))
    return max(int(dollars // price), 0)


def core_rebalance(checkpoint: str) -> list[dict[str, Any]]:
    """Top the core holding up toward its target. Returns the attempt records
    (empty when off, not this checkpoint, within the band, or halted)."""
    cfg = core_cfg()
    if not cfg or checkpoint != cfg.get("checkpoint", "midday"):
        return []
    symbol = str(cfg["symbol"]).upper()
    try:
        # A halt stops new buying; the core simply waits for the next day.
        if daily_loss_reason():
            return []

        from trading_agent.data.alpaca_client import get_account, get_latest_quote, get_positions

        account = get_account()
        equity = float(account["equity"])
        cash = float(account.get("cash") or 0.0)
        core_value = sum(
            abs(float(p.get("market_value") or 0.0))
            for p in get_positions()
            if str(p.get("symbol", "")).upper() == symbol
        )
        gap = core_gap_pct(equity, core_value, cfg)
        if gap < float(cfg.get("rebalance_band_pct", 5.0)):
            return []

        quote = get_latest_quote(symbol)
        ask = float(quote.get("ask_price") or 0.0)
        bid = float(quote.get("bid_price") or 0.0)
        price = ask or bid
        try:  # size at the fresh IEX last trade when there is one (see alpaca_client.get_live_price)
            from trading_agent.data.alpaca_client import get_live_price

            live, source = get_live_price(symbol)
            if live and source == "live_trade":
                price = float(live)
        except Exception:  # noqa: BLE001
            pass
        qty = core_order_qty(equity, cash, gap, price, cfg)
        if qty <= 0:
            result = {"ticker": symbol, "status": "skipped", "reason": "core top-up rounds to 0 shares or no cash",
                      "source": "core"}
            _persist_attempt(result, checkpoint)
            return [result]

        rec = {
            "ticker": symbol,
            "checkpoint": CORE_CHECKPOINT,
            "action": "BUY",
            "confidence": 100,
            "core": True,
            "reference_price": round((ask + bid) / 2, 6) if ask and bid else price,
            "reference_price_source": "live_mid" if ask and bid else "quote",
            "rationale": (
                f"Core holding: {symbol} is {core_value / equity * 100:.1f}% of the portfolio against a "
                f"{cfg['target_pct_of_portfolio']}% target; topping up in tranches."
            ),
        }
        record_decision(symbol, CORE_CHECKPOINT, "approve", {"qty": qty, "source": "core"})
        order = submit_approved_order(rec, qty, source="core")
        result = {"ticker": symbol, "status": "submitted", "qty": qty, "order": order, "source": "core"}
    except OrderRefused as exc:
        result = {"ticker": symbol, "status": "refused", "reason": str(exc), "source": "core"}
    except Exception as exc:  # noqa: BLE001 - the core must never break the checkpoint
        result = {"ticker": symbol, "status": "error", "reason": str(exc), "source": "core"}
    _persist_attempt(result, checkpoint)
    return [result]

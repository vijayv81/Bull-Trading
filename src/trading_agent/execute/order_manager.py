"""Order submission — strictly gated behind the kill switch and an approval
record whose terms match exactly (plan §8, hard requirement).

submit_approved_order() is the ONLY sanctioned path from a recommendation to
a live Alpaca call in this project. Nothing else in the codebase should call
data.alpaca_client.submit_market_order() directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trading_agent.config import TRADES_DIR, load_risk_limits
from trading_agent.data.alpaca_client import submit_market_order
from trading_agent.notify.approval_gateway import get_decision, is_expired
from trading_agent.utils import append_json, day_dir

SIZE_TOLERANCE_PCT = 1.0  # approved qty vs. requested qty must match within this %


class OrderRefused(Exception):
    """Raised whenever an order is NOT submitted — the expected outcome most
    of the time this module runs. Callers should treat this as normal control
    flow, not a bug."""


def submit_approved_order(rec: dict[str, Any], qty: float) -> dict[str, Any]:
    risk = load_risk_limits()["operational"]
    if not risk.get("trading_enabled", False):
        raise OrderRefused("Kill switch is off (config/risk_limits.yaml: trading_enabled=false).")

    decision = get_decision(rec["ticker"], rec["checkpoint"])
    if decision is None:
        raise OrderRefused(f"No approval record for {rec['ticker']} @ {rec['checkpoint']}.")
    if decision["decision"] != "approve":
        raise OrderRefused(
            f"Latest decision for {rec['ticker']} was '{decision['decision']}', not approve."
        )
    if is_expired(decision["timestamp"]):
        raise OrderRefused(f"Approval for {rec['ticker']} has expired.")

    approved_qty = decision.get("terms", {}).get("qty", qty)
    if approved_qty:
        drift_pct = abs(approved_qty - qty) / max(approved_qty, 1e-9) * 100
        if drift_pct > SIZE_TOLERANCE_PCT:
            raise OrderRefused(
                f"Requested qty {qty} does not match approved qty {approved_qty} "
                f"within {SIZE_TOLERANCE_PCT}% tolerance."
            )

    order = submit_market_order(rec["ticker"], rec["action"], qty)

    path = day_dir(TRADES_DIR) / "orders_submitted.json"
    append_json(
        path,
        {
            "recommendation": rec,
            "order": order,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return order

"""Instrument class: ordinary stock vs. blank-check (SPAC) unit vs. warrant/right.

Per user instruction 2026-10-10 ("what sort of protection or better strategy can
be used for blank check units stock in place of fundamentals"). A blank-check
company has no operations, so margins, growth and valuation mean nothing; what
it does have is a trust account, usually $10 a unit, which is the redemption
floor before a deal. So the price relative to that trust is the honest
"fundamental", and a unit trading at 3x the trust is priced on hype or bad
data, not value. Warrants and rights are option-like (this project bans
options outright) and swing double digits in minutes — the GRMLW/ABLVW/DAICW
losses of 2026-09 and the FVNNU fill of 2026-10-09.

Classified from the symbol alone, so it needs no network call and cannot fail:
a five-letter Nasdaq symbol's fifth letter is the issue type (U unit, W
warrant, R right). `position.instrument_overrides` in risk_limits.yaml maps a
symbol to "operating" (or another class) for the rare misclassification.
"""

from __future__ import annotations

from typing import Any

from trading_agent.config import load_risk_limits

OPERATING = "operating"
BLANK_CHECK_UNIT = "blank_check_unit"
WARRANT_OR_RIGHT = "warrant_or_right"

DEFAULTS = {
    "trust_value": 10.0,
    "max_premium_to_trust_pct": 15.0,
    "max_position_pct_of_portfolio": 1.0,
    "block_warrants_and_rights": True,
}


def blank_check_cfg() -> dict[str, Any]:
    return {**DEFAULTS, **(load_risk_limits().get("blank_check") or {})}


def instrument_class(symbol: str) -> str:
    symbol = symbol.upper()
    override = (load_risk_limits().get("position", {}).get("instrument_overrides") or {}).get(symbol)
    if override:
        return override
    if len(symbol) == 5 and symbol.isalpha():
        if symbol[-1] == "U":
            return BLANK_CHECK_UNIT
        if symbol[-1] in ("W", "R"):
            return WARRANT_OR_RIGHT
    return OPERATING


def trust_premium_pct(price: float, cfg: dict[str, Any] | None = None) -> float:
    trust = float((cfg or blank_check_cfg())["trust_value"])
    return (price / trust - 1) * 100


def blank_check_score(price: float | None, cfg: dict[str, Any] | None = None) -> float | None:
    """0-1 stand-in for the fundamental of a blank-check unit, from its price
    versus the trust value: 0.65 at or below the trust (the redemption floor
    limits the downside), sliding to 0.10 at `max_premium_to_trust_pct` and
    beyond (paying for a deal that may not happen). None without a price."""
    if not price or price <= 0:
        return None
    cfg = cfg or blank_check_cfg()
    premium = trust_premium_pct(price, cfg)
    cap = float(cfg["max_premium_to_trust_pct"]) or 15.0
    if premium <= 0:
        return 0.65
    return round(max(0.10, 0.65 - 0.55 * min(premium / cap, 1.0)), 4)

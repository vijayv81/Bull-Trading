"""Ties research -> data -> scoring -> notification into one checkpoint run
(plan §2). Each checkpoint is a fresh, self-contained run — state comes from
the file layer (plan §1.2 design principle #1), never from conversation or
session memory. This is what routines/ schedules via `/schedule`; see
routines/trading_checkpoints.md.

By default this module never executes a trade — it stops at notify(), and
approval/execution are separate, deliberately later steps (see
notify.approval_gateway and execute.order_manager, and `trading-agent
approvals` / `execute`). The one exception is execute.auto_pilot.auto_apply(),
gated behind config/risk_limits.yaml -> operational.auto_apply.enabled
(default off in spirit — see CLAUDE.md's "Auto-apply" section) — flipping
that back to false is a one-line revert to the description above being
unconditionally true again.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from trading_agent.config import load_watchlist
from trading_agent.data.alpaca_client import get_market_movers, get_positions, get_recent_bars
from trading_agent.execute.auto_pilot import auto_apply
from trading_agent.guardrails import RoutineHalted, daily_loss_reason, is_option_symbol
from trading_agent.notify.approval_gateway import notify_digest, save_recommendation
from trading_agent.research.perplexity_client import research_ticker
from trading_agent.scoring.recommendation_engine import (
    MIN_BARS_FOR_TECHNICAL,
    score_candidate,
    technical_score,
)

VALID_CHECKPOINTS = {"pre_open", "market_open", "midday", "pre_close"}

# Below this many actionable (BUY/SELL) candidates, an identical confidence
# across all of them is as likely to be coincidence as a data-quality failure
# — the check below only fires once there's enough of a sample to be sure a
# flat score isn't real.
FLAT_CONFIDENCE_MIN_CANDIDATES = 3


def _flat_confidence_alert(results: list[dict[str, Any]]) -> str | None:
    """Detect a degenerate run: every actionable candidate scoring the exact
    same confidence is never a real signal — sentiment/technical/catalyst
    inputs vary per ticker by construction, so identical confidence across
    several of them means an upstream input silently went constant (e.g. bars
    or research data), not that every ticker is equally attractive. Reported
    rather than trusted: notify_digest() surfaces it and run_checkpoint()
    skips auto_apply for the checkpoint when this fires.
    """
    actionable = [r for r in results if r.get("action") in ("BUY", "SELL")]
    if len(actionable) < FLAT_CONFIDENCE_MIN_CANDIDATES:
        return None
    confidences = {r["confidence"] for r in actionable}
    if len(confidences) > 1:
        return None
    return (
        f"All {len(actionable)} actionable recommendations this checkpoint scored an "
        f"identical confidence ({actionable[0]['confidence']}) — that is not a real signal, "
        f"it means a scoring input (sentiment/technical/catalyst) silently went constant "
        f"for every ticker. Auto-apply was skipped for this checkpoint; treat every "
        f"recommendation below as unverified until the cause is found."
    )


def _held_position_pnl_pct() -> dict[str, float]:
    """Every current Alpaca position's unrealized return, as a percent
    (Alpaca's own `unrealized_plpc` is a fraction, e.g. 0.05 = +5%). Used
    both to force continuous monitoring of anything already held (see
    run_checkpoint() below) and to feed score_candidate()'s stop-loss/
    take-profit bias. Best-effort: an unreadable account degrades to "no
    positions known" rather than blocking the checkpoint — the existing
    watchlist/movers research still runs either way, this only means a
    held position drops out of monitoring for this one run, same as any
    other best-effort Alpaca read in this module (get_market_movers()).
    """
    try:
        return {
            p["symbol"]: round(float(p.get("unrealized_plpc") or 0.0) * 100, 2)
            for p in get_positions()
            if p.get("symbol")
        }
    except Exception as exc:  # noqa: BLE001 - one bad read must not block the checkpoint
        print(f"Could not read current positions for continuous monitoring: {exc}")
        return {}


def run_checkpoint(checkpoint: str, extra_tickers: list[str] | None = None) -> list[dict[str, Any]]:
    if checkpoint not in VALID_CHECKPOINTS:
        raise ValueError(f"Unknown checkpoint {checkpoint!r} — expected one of {VALID_CHECKPOINTS}")

    # Halt before spending any research budget: past the daily loss cap there is
    # nothing this checkpoint should be proposing.
    halt = daily_loss_reason()
    if halt:
        raise RoutineHalted(halt)

    held_pnl = _held_position_pnl_pct()

    tickers = set(load_watchlist()) | set(extra_tickers or [])
    movers = get_market_movers()
    # Movers only *seed* candidates (plan §4) — they go through the exact same
    # research, scoring, and (if enabled) auto_apply path as the core
    # watchlist. A mover isn't inherently more or less trusted than a
    # watchlist ticker; nothing here treats it specially.
    tickers |= {m["symbol"] for m in movers.get("gainers", [])[:10] if "symbol" in m}
    # Continuous monitoring: anything currently held stays in the research
    # universe every checkpoint regardless of watchlist/movers, so a position
    # bought today (possibly not on the watchlist at all) never silently
    # drops out of scoring the moment it stops being a "mover" — without
    # this, no future checkpoint would ever propose a SELL for it again.
    tickers |= set(held_pnl)
    # Never research, score, or propose an options contract, whatever the source.
    tickers = {t for t in tickers if not is_option_symbol(t)}

    results = []
    failed_tickers = []
    for ticker in sorted(tickers):
        try:
            research = research_ticker(ticker, checkpoint)

            bars = pd.DataFrame(get_recent_bars(ticker))
            # Fewer bars than the SMA window means technical_score() would only
            # ever return its neutral 0.5 fallback — that's not a real reading,
            # so score_candidate() must exclude it (None) rather than treat a
            # placeholder as this ticker's actual technical signal.
            tech = technical_score(bars) if len(bars) >= MIN_BARS_FOR_TECHNICAL else None

            rec = score_candidate(
                ticker=ticker,
                checkpoint=checkpoint,
                sentiment=research.get("confidence_of_extraction", 0.5),
                technical=tech,
                fundamental=None,  # no fundamentals vendor wired up yet — excluded from the score, not neutral
                catalyst=0.7 if research.get("sources") else 0.3,
                position_pnl_pct=held_pnl.get(ticker),
            )
            rec["rationale"] = (research.get("headline_summary") or "")[:280]
            rec["sources"] = research.get("sources", [])
            rec["research_source"] = research.get("research_source", "perplexity")

            save_recommendation(rec)
            results.append(rec)
        except Exception as exc:  # noqa: BLE001 - one ticker's failure must not kill the checkpoint
            failed_tickers.append({"ticker": ticker, "error": str(exc)})
            print(f"SKIPPED {ticker}: research/scoring failed — {exc}")

    data_quality_alert = _flat_confidence_alert(results)
    if data_quality_alert:
        print(f"DATA QUALITY ALERT: {data_quality_alert}")
        auto_results: list[dict[str, Any]] = []
    else:
        auto_results = auto_apply(results, checkpoint)
    notify_digest(
        results,
        checkpoint,
        auto_results=auto_results,
        failed_tickers=failed_tickers,
        data_quality_alert=data_quality_alert,
    )
    return results

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
from trading_agent.data.alpaca_client import get_market_movers, get_recent_bars
from trading_agent.execute.auto_pilot import auto_apply
from trading_agent.guardrails import RoutineHalted, daily_loss_reason, is_option_symbol
from trading_agent.notify.approval_gateway import notify_digest, save_recommendation
from trading_agent.research.perplexity_client import research_ticker
from trading_agent.scoring.recommendation_engine import score_candidate, technical_score

VALID_CHECKPOINTS = {"pre_open", "market_open", "midday", "pre_close"}


def run_checkpoint(checkpoint: str, extra_tickers: list[str] | None = None) -> list[dict[str, Any]]:
    if checkpoint not in VALID_CHECKPOINTS:
        raise ValueError(f"Unknown checkpoint {checkpoint!r} — expected one of {VALID_CHECKPOINTS}")

    # Halt before spending any research budget: past the daily loss cap there is
    # nothing this checkpoint should be proposing.
    halt = daily_loss_reason()
    if halt:
        raise RoutineHalted(halt)

    tickers = set(load_watchlist()) | set(extra_tickers or [])
    movers = get_market_movers()
    # Movers only *seed* candidates (plan §4) — they go through the exact same
    # research, scoring, and (if enabled) auto_apply path as the core
    # watchlist. A mover isn't inherently more or less trusted than a
    # watchlist ticker; nothing here treats it specially.
    tickers |= {m["symbol"] for m in movers.get("gainers", [])[:10] if "symbol" in m}
    # Never research, score, or propose an options contract, whatever the source.
    tickers = {t for t in tickers if not is_option_symbol(t)}

    results = []
    failed_tickers = []
    for ticker in sorted(tickers):
        try:
            research = research_ticker(ticker, checkpoint)

            bars = pd.DataFrame(get_recent_bars(ticker))
            tech = technical_score(bars)

            rec = score_candidate(
                ticker=ticker,
                checkpoint=checkpoint,
                sentiment=research.get("confidence_of_extraction", 0.5),
                technical=tech,
                fundamental=None,  # no fundamentals vendor wired up yet — excluded from the score, not neutral
                catalyst=0.7 if research.get("sources") else 0.3,
            )
            rec["rationale"] = (research.get("headline_summary") or "")[:280]
            rec["sources"] = research.get("sources", [])

            save_recommendation(rec)
            results.append(rec)
        except Exception as exc:  # noqa: BLE001 - one ticker's failure must not kill the checkpoint
            failed_tickers.append({"ticker": ticker, "error": str(exc)})
            print(f"SKIPPED {ticker}: research/scoring failed — {exc}")

    auto_results = auto_apply(results, checkpoint)
    notify_digest(results, checkpoint, auto_results=auto_results, failed_tickers=failed_tickers)
    return results

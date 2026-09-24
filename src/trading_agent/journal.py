"""Decision + outcome journal — the measurement half of the improvement loop
(plan §6.3, the piece CLAUDE.md lists as not built).

`data/recommendations/` records what the pipeline proposed and
`data/approvals/` records what the human decided. Neither records *why* the
human decided it, and nothing records what the price subsequently did. Without
those two, `historical_hitrate()` can only ever return its neutral 0.5 default
and `propose_weight_adjustments()` has nothing to propose from.

An entry is written at decision time with the reasoning in the user's own words
and a reference price; `mark_outcomes()` fills in the result later. Entries are
kept as tracked audit data alongside the rest of data/, so a decision's stated
reasoning can't be quietly rewritten after the outcome is known.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_agent.config import DATA_DIR, PERFORMANCE_DIR, RECOMMENDATIONS_DIR
from trading_agent.utils import append_json, day_dir, load_json_list, today

JOURNAL_DIR = DATA_DIR / "journal"


def _strategy_metrics_path() -> Path:
    return PERFORMANCE_DIR / "strategy_metrics.json"


def _entries_path(day: str | None = None) -> Path:
    return day_dir(JOURNAL_DIR, day) / "entries.json"


def _reference_price(ticker: str) -> float | None:
    """Mid price for outcome measurement, or None when quotes are unavailable.

    Deliberately soft: a missing quote must not block recording the reasoning,
    which is the part that can't be reconstructed later. mark_outcomes() will
    skip entries with no reference price rather than guessing one.
    """
    try:
        from trading_agent.data.alpaca_client import get_latest_quote

        quote = get_latest_quote(ticker)
        bid = float(quote.get("bid_price") or 0.0)
        ask = float(quote.get("ask_price") or 0.0)
        prices = [p for p in (bid, ask) if p > 0]
        return sum(prices) / len(prices) if prices else None
    except Exception:
        return None


def record_entry(
    ticker: str,
    checkpoint: str,
    decision: str,
    reasoning: str,
    action: str | None = None,
    confidence: float | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Journal one human decision, in their words, with a price to measure from."""
    entry = {
        "ticker": ticker.upper(),
        "checkpoint": checkpoint,
        "decision": decision,
        "action": action,
        "confidence": confidence,
        "reasoning": reasoning,
        "reference_price": _reference_price(ticker),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "outcome": None,
    }
    append_json(_entries_path(day), entry)
    return entry


def load_entries(day: str | None = None) -> list[dict[str, Any]]:
    return load_json_list(_entries_path(day))


def _directionally_correct(action: str | None, pct_change: float) -> bool | None:
    """Was the call right about direction? None when there was no direction to be right about."""
    if action is None:
        return None
    action = action.upper()
    if action == "BUY":
        return pct_change > 0
    if action == "SELL":
        return pct_change < 0
    return None


def mark_outcomes(day: str | None = None) -> list[dict[str, Any]]:
    """Fill in the outcome for entries that have a reference price but no result yet.

    Re-runnable: entries already marked are left alone, so this can run at every
    pre_close without double-counting or overwriting an earlier measurement.
    """
    day = day or today()
    entries = load_entries(day)
    updated = []

    for entry in entries:
        if entry.get("outcome") is not None:
            continue
        reference = entry.get("reference_price")
        if not reference:
            continue

        current = _reference_price(entry["ticker"])
        if not current:
            continue

        pct_change = (current - reference) / reference * 100
        entry["outcome"] = {
            "price_at_decision": reference,
            "price_at_measurement": current,
            "pct_change": round(pct_change, 2),
            "directionally_correct": _directionally_correct(entry.get("action"), pct_change),
            "measured_at": datetime.now(timezone.utc).isoformat(),
        }
        updated.append(entry)

    if updated:
        _entries_path(day).write_text(json.dumps(entries, indent=2, default=str))
    return updated


def _journal_days() -> list[str]:
    """Every date-named subdirectory under data/journal/, oldest first."""
    if not JOURNAL_DIR.exists():
        return []
    return sorted(p.name for p in JOURNAL_DIR.iterdir() if p.is_dir())


def _recommendations_index(day: str) -> dict[tuple[str, str], dict[str, Any]]:
    """(checkpoint, ticker) -> that day's recommendation record, for pulling
    component_scores back up for an entry that only carries ticker/checkpoint."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    day_path = RECOMMENDATIONS_DIR / day
    if not day_path.exists():
        return index
    for path in sorted(day_path.glob("recs_*.json")):
        for rec in load_json_list(path):
            index[(rec["checkpoint"], rec["ticker"])] = rec
    return index


def _hit_rate_block(verdicts: list[bool]) -> dict[str, Any]:
    return {"hit_rate": round(sum(verdicts) / len(verdicts), 4), "n": len(verdicts)}


def aggregate_performance(days: list[str] | None = None) -> dict[str, Any]:
    """Roll journaled, outcome-marked decisions up into data/performance/
    strategy_metrics.json — the file historical_hitrate() and
    propose_weight_adjustments() read (plan §6.3, the last hop of the
    improvement loop).

    A full recompute from data/journal/ + data/recommendations/ every call,
    never an incremental merge — so it can't drift from the source data, and
    re-running it after correcting a bad journal entry just works. Only
    entries with a `directionally_correct` verdict (mark_outcomes() has run,
    and the decision had a BUY/SELL direction to be right or wrong about)
    count anywhere.

    by_ticker: hit rate per ticker, from the entry's own outcome — how often
    THIS ticker's calls were directionally correct.

    by_signal_type: hit rate per scoring component (sentiment, technical, ...),
    found by looking the entry's ticker+checkpoint back up in that day's
    data/recommendations/ for its component_scores. A component "hit" means
    its own lean (>=0.5 bullish, <0.5 bearish) agreed with which way the price
    actually moved — independent of what the blended action ended up being,
    so a signal that leans right even inside a losing call still gets credit,
    and a signal that leans wrong inside a winning one doesn't. A component
    recorded as None (no data yet — see recommendation_engine.score_candidate)
    is skipped, same as it's excluded from the confidence sum itself.
    """
    days = days if days is not None else _journal_days()

    ticker_verdicts: dict[str, list[bool]] = {}
    signal_verdicts: dict[str, list[bool]] = {}

    for day in days:
        recs = _recommendations_index(day)
        for entry in load_entries(day):
            outcome = entry.get("outcome")
            if not outcome:
                continue
            correct = outcome.get("directionally_correct")
            if correct is None:
                continue

            ticker_verdicts.setdefault(entry["ticker"], []).append(correct)

            rec = recs.get((entry["checkpoint"], entry["ticker"]))
            if not rec:
                continue
            actual_bullish = outcome["pct_change"] > 0
            for signal, score in rec.get("component_scores", {}).items():
                if score is None:
                    continue
                signal_leaned_bullish = score >= 0.5
                signal_verdicts.setdefault(signal, []).append(signal_leaned_bullish == actual_bullish)

    metrics = {
        "_comment": (
            "Generated by journal.aggregate_performance() from data/journal + "
            "data/recommendations — never hand-edit, re-run instead."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "by_ticker": {t: _hit_rate_block(v) for t, v in ticker_verdicts.items()},
        "by_signal_type": {s: _hit_rate_block(v) for s, v in signal_verdicts.items()},
    }
    path = _strategy_metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, indent=2, default=str))
    return metrics

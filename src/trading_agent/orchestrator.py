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

from trading_agent.config import load_risk_limits, load_watchlist
from trading_agent.data.alpaca_client import (
    get_market_movers,
    get_market_return_pct,
    get_positions,
    get_recent_bars,
)
from trading_agent.execute.auto_pilot import auto_apply
from trading_agent.guardrails import RoutineHalted, daily_loss_reason, is_option_symbol
from trading_agent.notify.approval_gateway import notify_digest, save_recommendation
from trading_agent.research.perplexity_client import research_ticker
from trading_agent.scoring.recommendation_engine import (
    MIN_BARS_FOR_TECHNICAL,
    TREND_FETCH_CALENDAR_DAYS,
    long_term_trend,
    score_candidate,
    technical_score,
)
from trading_agent.scoring.text_signals import catalyst_score, sentiment_score

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


def _above_min_price(movers: list[dict[str, Any]], min_price: float | None) -> list[dict[str, Any]]:
    """Mover entries at or above `min_price`, preserving order (Alpaca
    already returns gainers/losers ranked by % move, and run_checkpoint()
    takes the top 10 of whatever this returns). `min_price` unset/zero
    means no floor — same unset-means-uncapped convention as every
    guardrail cap in this project. A mover with no price at all (shouldn't
    happen from Alpaca's screener, but data is data) is excluded rather
    than assumed to pass, the same conservative default as every other
    "can't verify" case here — this is candidate *sourcing*, not an order
    guardrail, so there's no fail-closed/fail-open distinction to get
    wrong either way: skipping one just means fewer candidates researched.
    """
    if not min_price:
        return movers
    return [m for m in movers if (m.get("price") or 0) >= min_price]


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


def _score_tickers(
    tickers: list[str],
    checkpoint: str,
    held_pnl: dict[str, float],
    market_return_pct: float | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The research -> bars -> score body for one batch of tickers — shared
    by run_checkpoint()'s two passes (held positions, then watchlist/movers;
    see run_checkpoint() for why the split exists) so it isn't duplicated.
    """
    results: list[dict[str, Any]] = []
    failed_tickers: list[dict[str, Any]] = []
    for ticker in tickers:
        try:
            research = research_ticker(ticker, checkpoint)

            # TREND_FETCH_CALENDAR_DAYS (~52 weeks) instead of just enough for
            # MIN_BARS_FOR_TECHNICAL's SMA window, so technical_score() and
            # long_term_trend() share one Alpaca bars fetch per ticker rather
            # than needing two.
            bars = pd.DataFrame(get_recent_bars(ticker, lookback_days=TREND_FETCH_CALENDAR_DAYS))
            # Fewer bars than the SMA window means technical_score() would only
            # ever return its neutral 0.5 fallback — that's not a real reading,
            # so score_candidate() must exclude it (None) rather than treat a
            # placeholder as this ticker's actual technical signal.
            have_bars = len(bars) >= MIN_BARS_FOR_TECHNICAL
            tech = technical_score(bars) if have_bars else None
            # The price technical_score() actually read — recorded so a later
            # order submission can catch the market having moved since
            # (guardrails.stale_recommendation_reason()).
            reference_price = float(bars["close"].iloc[-1]) if have_bars else None
            # None below MIN_BARS_FOR_TREND (its own, stricter bar count) —
            # long_term_trend() handles that itself, no extra guard needed here.
            trend = long_term_trend(bars)

            headline_text = research.get("headline_summary") or ""
            rec = score_candidate(
                ticker=ticker,
                checkpoint=checkpoint,
                # Keyword-derived from what the research actually said, not
                # from whether the research call merely succeeded — see
                # scoring/text_signals.py. None (excluded, not faked) when
                # the text has no detectable sentiment/catalyst language.
                sentiment=sentiment_score(headline_text),
                technical=tech,
                fundamental=None,  # no fundamentals vendor wired up yet — excluded from the score, not neutral
                catalyst=catalyst_score(headline_text),
                position_pnl_pct=held_pnl.get(ticker),
                reference_price=reference_price,
                long_term_trend_ctx=trend,
                market_return_pct=market_return_pct,
            )
            rec["rationale"] = (research.get("headline_summary") or "")[:280]
            rec["sources"] = research.get("sources", [])
            rec["research_source"] = research.get("research_source", "perplexity")

            save_recommendation(rec)
            results.append(rec)
        except Exception as exc:  # noqa: BLE001 - one ticker's failure must not kill the checkpoint
            failed_tickers.append({"ticker": ticker, "error": str(exc)})
            print(f"SKIPPED {ticker}: research/scoring failed — {exc}")
    return results, failed_tickers


def run_checkpoint(checkpoint: str, extra_tickers: list[str] | None = None) -> list[dict[str, Any]]:
    if checkpoint not in VALID_CHECKPOINTS:
        raise ValueError(f"Unknown checkpoint {checkpoint!r} — expected one of {VALID_CHECKPOINTS}")

    # Halt before spending any research budget: past the daily loss cap there is
    # nothing this checkpoint should be proposing.
    halt = daily_loss_reason()
    if halt:
        raise RoutineHalted(halt)

    held_pnl = _held_position_pnl_pct()
    # Read once per checkpoint (not once per ticker) and handed to every held
    # position's scoring call for score_candidate()'s market-regime stop-loss
    # dampening. Best-effort, same tolerance as every other Alpaca read here:
    # an unreadable benchmark degrades to "no regime context" (None), never
    # blocks the checkpoint.
    market_return_pct = get_market_return_pct("SPY")

    watchlist_tickers = set(load_watchlist()) | set(extra_tickers or [])
    movers = get_market_movers()
    # Movers only *seed* candidates (plan §4) — they go through the exact same
    # research, scoring, and (if enabled) auto_apply path as the core
    # watchlist. A mover isn't inherently more or less trusted than a
    # watchlist ticker; nothing here treats it specially. Both gainers AND
    # losers — get_market_movers() has always fetched both, but only
    # gainers were ever used here, so the candidate universe was 100% biased
    # toward names already up (compounding the momentum-chasing risk
    # MOMENTUM_CAP exists to bound, not counteracting it) and the same
    # narrow slice of tickers dominated day after day. Per user instruction
    # 2026-09-30 ("ensure assessment is done broadly"). A loser goes through
    # the exact same scoring as a gainer — nothing here assumes a falling
    # price means a buy OR a sell, `technical_score()` still decides that.
    #
    # Filtered by position.candidate_min_price BEFORE taking the top 10 of
    # each (not after — filtering post-slice would just shrink the list on
    # a penny-stock-heavy day instead of reaching past them for real
    # candidates). This is a liquidity floor on *new* candidate sourcing
    # only — see below for why held positions are deliberately exempt.
    min_price = load_risk_limits().get("position", {}).get("candidate_min_price")
    gainers = _above_min_price(movers.get("gainers", []), min_price)
    losers = _above_min_price(movers.get("losers", []), min_price)
    watchlist_tickers |= {m["symbol"] for m in gainers[:10] if "symbol" in m}
    watchlist_tickers |= {m["symbol"] for m in losers[:10] if "symbol" in m}
    # Never research, score, or propose an options contract, whatever the source.
    watchlist_tickers = {t for t in watchlist_tickers if not is_option_symbol(t)}

    # Continuous monitoring: anything currently held is scored every
    # checkpoint regardless of watchlist/movers status, so a position bought
    # today (possibly not on the watchlist at all) never silently drops out
    # of scoring the moment it stops being a "mover" — without this, no
    # future checkpoint would ever propose a SELL for it again.
    #
    # Scored in its OWN, earlier pass — and auto_apply() called on it
    # immediately, before the (usually larger) watchlist/movers batch is
    # even researched — so a stop-loss/take-profit exit gets the freshest
    # possible price/technical re-check by the time execute.order_manager's
    # stale_recommendation_reason() looks at it. Under the old single-pass,
    # single-auto_apply-call design, a held position researched early in a
    # long ticker loop could sit for several minutes before auto_apply ever
    # got to it, and by then the market had often already moved past
    # execution.max_price_drift_pct — confirmed live 2026-09-29, where every
    # stop-loss-triggered SELL that checkpoint was refused. Held positions
    # are removed from the watchlist/movers batch so nothing is scored twice.
    held_tickers = {t for t in held_pnl if not is_option_symbol(t)}
    watchlist_tickers -= held_tickers

    results: list[dict[str, Any]] = []
    failed_tickers: list[dict[str, Any]] = []
    auto_results: list[dict[str, Any]] = []
    data_quality_alerts: list[str] = []

    for batch in (held_tickers, watchlist_tickers):
        if not batch:
            continue
        batch_results, batch_failed = _score_tickers(sorted(batch), checkpoint, held_pnl, market_return_pct)
        results.extend(batch_results)
        failed_tickers.extend(batch_failed)

        alert = _flat_confidence_alert(batch_results)
        if alert:
            print(f"DATA QUALITY ALERT: {alert}")
            data_quality_alerts.append(alert)
            continue  # a flat score never reaches auto_apply for this batch
        auto_results.extend(auto_apply(batch_results, checkpoint))

    notify_digest(
        results,
        checkpoint,
        auto_results=auto_results,
        failed_tickers=failed_tickers,
        data_quality_alert=" | ".join(data_quality_alerts) or None,
    )
    return results

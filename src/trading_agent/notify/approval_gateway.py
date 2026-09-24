"""Notification + approval workflow (plan §8, hard requirement).

execute/order_manager.py refuses to submit any order without a matching,
timestamped record written here — nothing in this module executes a trade,
and record_decision() itself never decides anything, it only persists
whatever decision its caller already made. By default that caller is always
a human (`trading-agent approvals approve/reject`); the one exception is
execute.auto_pilot, gated behind its own config flag — see that module and
CLAUDE.md's "Auto-apply" section. A recommendation with no response within
config/risk_limits.yaml -> operational.approval_expiry_hours simply expires
(see is_expired()); callers must check that themselves before treating an
old "approve" as still valid.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from trading_agent.config import APPROVALS_DIR, RECOMMENDATIONS_DIR, load_agent_config, load_risk_limits
from trading_agent.utils import append_json, day_dir, load_json_list

Decision = Literal["approve", "reject"]


def save_recommendation(rec: dict[str, Any]) -> None:
    """Persist a recommendation and notify. This never executes anything —
    it's step 1-2 of the plan §8 flow (generate -> notify), execution needs a
    separate, later approval.
    """
    path = day_dir(RECOMMENDATIONS_DIR) / f"recs_{rec['checkpoint']}.json"
    append_json(path, rec)
    notify(rec)


def notification_channels() -> set[str]:
    """Normalize notifications.channel (a string or a list) into a lowercase
    set. Unknown values pass through harmlessly — nothing matches them."""
    channel = load_agent_config().get("notifications", {}).get("channel", "console")
    if isinstance(channel, str):
        return {channel}
    return {str(c) for c in channel}


def notify(rec: dict[str, Any]) -> None:
    """Per-ticker notification for the console/file channels (plan §12).
    Email and SMS are handled once per checkpoint by notify_digest() instead
    of once per ticker — see run_checkpoint() — so they aren't repeated here.
    """
    channels = notification_channels()
    if "console" in channels:
        message = (
            f"[{rec['checkpoint']}] {rec['action']} {rec['ticker']} "
            f"(confidence {rec['confidence']}) — {rec.get('rationale', '')}"
        )
        print(f"NOTIFY: {message}")
    # "file" channel: the recommendation JSON written by save_recommendation()
    # IS the notification — nothing further to do.


def notify_digest(
    recs: list[dict[str, Any]],
    checkpoint: str,
    auto_results: list[dict[str, Any]] | None = None,
    failed_tickers: list[dict[str, Any]] | None = None,
) -> None:
    """One consolidated email/SMS per checkpoint instead of one per ticker —
    a mover-heavy checkpoint would otherwise fire a dozen texts. Console/file
    already got every recommendation individually via notify(); this only
    fires for the "email"/"sms" channels.

    `auto_results` (execute.auto_pilot.auto_apply()'s return, when that's
    enabled) gets its own section — a human reading this must be able to tell
    a recommendation the system already acted on apart from one still waiting
    on them, never have to guess. `failed_tickers` (orchestrator.run_checkpoint()'s
    per-ticker research/scoring failures) gets one too, for the same reason: a
    quiet checkpoint and a checkpoint that silently dropped half the watchlist
    to a research API error must not read the same to a human skimming this.

    A send failure (bad API key, unreachable host) is reported, not
    raised — a broken notification channel should never halt the pipeline
    that produced the recommendations it was trying to deliver.
    """
    channels = notification_channels()
    if not channels & {"email", "sms"}:
        return

    actionable = [r for r in recs if r.get("action") in ("BUY", "SELL")]
    lines = [
        f"{r['action']} {r['ticker']} (confidence {r['confidence']}) — {r.get('rationale', '')[:120]}"
        for r in actionable
    ]
    subject = f"[Bull-Trading] {checkpoint}: {len(actionable)} recommendation(s)"
    body = "\n".join(lines) if lines else "No actionable recommendations this checkpoint (all HOLD, or nothing scored)."

    if auto_results:
        body += "\n\nAuto-applied:\n" + "\n".join(_auto_result_line(r) for r in auto_results)

    if failed_tickers:
        body += "\n\nResearch failed (skipped, not scored):\n" + "\n".join(
            f"- {f['ticker']}: {f['error'][:120]}" for f in failed_tickers
        )

    if "email" in channels:
        try:
            from trading_agent.notify.senders import send_email

            send_email(subject, body)
        except Exception as exc:  # noqa: BLE001 - a broken channel must not halt the routine
            print(f"NOTIFY (email) failed: {exc}")

    # SMS is skipped entirely on a quiet checkpoint — nothing here is worth a text.
    if "sms" in channels and (actionable or auto_results):
        try:
            from trading_agent.notify.senders import send_sms

            sms_parts = [f"{r['action']} {r['ticker']} ({r['confidence']})" for r in actionable]
            if auto_results:
                submitted = [r for r in auto_results if r["status"] == "submitted"]
                if submitted:
                    sms_parts.append(
                        "AUTO: " + ", ".join(f"{r['ticker']} x{r['qty']}" for r in submitted)
                    )
            sms_body = "Bull-Trading " + checkpoint + ": " + "; ".join(sms_parts)
            send_sms(sms_body[:300])
        except Exception as exc:  # noqa: BLE001
            print(f"NOTIFY (sms) failed: {exc}")


def _auto_result_line(result: dict[str, Any]) -> str:
    status = result["status"]
    if status == "submitted":
        return f"- {result['ticker']}: SUBMITTED qty={result['qty']}"
    return f"- {result['ticker']}: {status} — {result.get('reason', '')}"


def notify_daily_summary(summary: dict[str, Any]) -> None:
    """End-of-day learnings + benchmark comparison email/SMS (plan §12) — one
    per day, separate from notify_digest()'s per-checkpoint messages. `summary`
    is reporting.report_builder.build_daily_summary()'s return.

    Config-gated independently of the channel list itself:
    notifications.daily_summary_enabled (default true) — false is a one-line
    way to keep per-checkpoint digests without the end-of-day summary, no
    code change needed. A send failure is reported, not raised, same as
    notify_digest().
    """
    if not load_agent_config().get("notifications", {}).get("daily_summary_enabled", True):
        return
    channels = notification_channels()
    if not channels & {"email", "sms"}:
        return

    lines = [f"Bull-Trading daily summary — {summary['day']}", ""]
    portfolio_pct = summary["portfolio_return_pct"]
    benchmark_pct = summary["benchmark_return_pct"]
    if portfolio_pct is not None:
        lines.append(f"Portfolio: {portfolio_pct:+.2f}%")
    if benchmark_pct is not None:
        lines.append(f"{summary['benchmark_symbol']}: {benchmark_pct:+.2f}%")
    if summary["outperformance_pct"] is not None:
        lines.append(f"Vs. {summary['benchmark_symbol']}: {summary['outperformance_pct']:+.2f} pts")
    if portfolio_pct is None or benchmark_pct is None:
        lines.append("(one or both returns unavailable today — see the weekly report's P&L section instead)")

    lines.append(f"Recommendations: {summary['recommendations_count']} | Trades: {summary['trades_count']}")

    entries = summary["journal_entries"]
    lines.append("")
    if entries:
        lines.append("Findings:")
        verdict_label = {True: "correct", False: "wrong", None: "n/a"}
        for e in entries:
            outcome = e.get("outcome") or {}
            verdict = verdict_label[outcome.get("directionally_correct")]
            lines.append(f"- {e['ticker']} [{e['checkpoint']}] {e['decision']} — {verdict}: {e['reasoning'][:100]}")
    else:
        lines.append("No journaled decisions today.")

    subject = f"[Bull-Trading] Daily summary — {summary['day']}"
    body = "\n".join(lines)

    if "email" in channels:
        try:
            from trading_agent.notify.senders import send_email

            send_email(subject, body)
        except Exception as exc:  # noqa: BLE001 - a broken channel must not halt the routine
            print(f"NOTIFY (daily summary email) failed: {exc}")

    if "sms" in channels:
        try:
            from trading_agent.notify.senders import send_sms

            if portfolio_pct is not None and benchmark_pct is not None:
                headline = (
                    f"Bull-Trading {summary['day']}: {portfolio_pct:+.2f}% "
                    f"vs {summary['benchmark_symbol']} {benchmark_pct:+.2f}%"
                )
            else:
                headline = f"Bull-Trading {summary['day']}: daily summary sent by email"
            send_sms(headline[:300])
        except Exception as exc:  # noqa: BLE001
            print(f"NOTIFY (daily summary sms) failed: {exc}")


def record_decision(
    ticker: str, checkpoint: str, decision: Decision, terms: dict[str, Any] | None = None
) -> None:
    """Record a human approve/reject decision — the only thing that can ever
    unlock order submission for this ticker/checkpoint (plan §8, step 3-4).
    """
    path = day_dir(APPROVALS_DIR) / f"decisions_{checkpoint}.json"
    append_json(
        path,
        {
            "ticker": ticker,
            "checkpoint": checkpoint,
            "decision": decision,
            "terms": terms or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def get_decision(ticker: str, checkpoint: str) -> dict[str, Any] | None:
    """Most recent decision for this ticker+checkpoint today, or None if the
    recommendation hasn't been responded to yet.
    """
    path = day_dir(APPROVALS_DIR) / f"decisions_{checkpoint}.json"
    matches = [d for d in load_json_list(path) if d["ticker"] == ticker]
    return matches[-1] if matches else None


def is_expired(decision_timestamp: str) -> bool:
    expiry_hours = load_risk_limits()["operational"]["approval_expiry_hours"]
    ts = datetime.fromisoformat(decision_timestamp)
    return datetime.now(timezone.utc) - ts > timedelta(hours=expiry_hours)


def list_pending(checkpoint: str) -> list[dict[str, Any]]:
    """Recommendations from today's checkpoint that have no decision yet —
    what a human should be looking at right now.
    """
    rec_path = day_dir(RECOMMENDATIONS_DIR) / f"recs_{checkpoint}.json"
    recs = load_json_list(rec_path)
    decided_tickers = {
        d["ticker"] for d in load_json_list(day_dir(APPROVALS_DIR) / f"decisions_{checkpoint}.json")
    }
    return [r for r in recs if r["ticker"] not in decided_tickers]

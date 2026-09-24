"""Notification + approval workflow (plan §8, hard requirement).

execute/order_manager.py refuses to submit any order without a matching,
timestamped record written here — nothing in this module ever executes a
trade, and nothing auto-approves. A recommendation with no response within
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


def notify_digest(recs: list[dict[str, Any]], checkpoint: str) -> None:
    """One consolidated email/SMS per checkpoint instead of one per ticker —
    a mover-heavy checkpoint would otherwise fire a dozen texts. Console/file
    already got every recommendation individually via notify(); this only
    fires for the "email"/"sms" channels.

    A send failure (bad SMTP creds, unreachable host) is reported, not
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

    if "email" in channels:
        try:
            from trading_agent.notify.senders import send_email

            send_email(subject, body)
        except Exception as exc:  # noqa: BLE001 - a broken channel must not halt the routine
            print(f"NOTIFY (email) failed: {exc}")

    # SMS is skipped entirely on a quiet checkpoint — nothing here is worth a text.
    if "sms" in channels and actionable:
        try:
            from trading_agent.notify.senders import send_sms

            sms_body = "Bull-Trading " + checkpoint + ": " + "; ".join(
                f"{r['action']} {r['ticker']} ({r['confidence']})" for r in actionable
            )
            send_sms(sms_body[:300])
        except Exception as exc:  # noqa: BLE001
            print(f"NOTIFY (sms) failed: {exc}")


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

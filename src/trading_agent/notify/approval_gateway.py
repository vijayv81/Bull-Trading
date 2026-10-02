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
from pathlib import Path
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
    data_quality_alert: str | None = None,
) -> None:
    """One consolidated email/SMS per checkpoint instead of one per ticker —
    a mover-heavy checkpoint would otherwise fire a dozen texts. Console/file
    already got every recommendation individually via notify(); this only
    fires for the "email"/"sms" channels.

    Per user instruction 2026-09-30 ("crisp and mobile friendly ... only
    show actionable items ... if any stock has no analysis info, it should
    be skipped"):
    - A recommendation with no real analysis behind it (blank `rationale` —
      a research call that technically succeeded but produced no usable
      summary) is dropped from the actionable list entirely, rather than
      shown as an empty-looking entry.
    - `failed_tickers` (orchestrator.run_checkpoint()'s per-ticker
      research/scoring failures) is no longer shown in the email/SMS at
      all — it isn't actionable, and it's already visible where it happens
      (the checkpoint session's own console output); this digest is for
      what a human needs to act on, not an error log.
    - A checkpoint with nothing actionable, nothing auto-applied, and no
      data-quality problem sends nothing at all, on either channel — same
      "silence is fine on a quiet run" convention notify_pending_reminder()
      already uses, rather than a "nothing to see here" email every time.
    - The email carries a mobile-friendly HTML rendering (notify.html)
      alongside the plain-text body senders.send_email() already sent —
      plain text is unchanged as the fallback for clients that strip HTML.

    `auto_results` (execute.auto_pilot.auto_apply()'s return, when that's
    enabled) gets its own section — a human reading this must be able to
    tell a recommendation the system already acted on apart from one still
    waiting on them, never have to guess.

    `data_quality_alert` (orchestrator._flat_confidence_alert()'s return) is
    printed to console unconditionally, independent of the configured
    notification channel — this is exactly the run where trusting only the
    "email" channel would be the mistake, so it can't be silenced by channel
    config the way the rest of this function's output can. Unlike a quiet
    checkpoint, this always sends even with nothing else actionable — it's
    a warning, not routine noise.

    `portfolio.max_new_proposals_per_checkpoint` (declared from the start,
    never enforced anywhere until now) caps how many actionable
    recommendations this digest headlines, ranked by confidence — a
    mover-heavy checkpoint surfacing a dozen actionable calls at once was
    exactly the notification fatigue that key's own comment already
    described. Every recommendation is still saved to
    data/recommendations/ regardless (the audit trail stays complete); this
    only trims what the email/SMS actually leads with. Unset/zero means no
    cap, same convention as every other guardrail here.

    A send failure (bad API key, unreachable host) is reported, not
    raised — a broken notification channel should never halt the pipeline
    that produced the recommendations it was trying to deliver.
    """
    if data_quality_alert:
        print(f"NOTIFY (data quality): {data_quality_alert}")

    channels = notification_channels()
    if not channels & {"email", "sms"}:
        return

    actionable = [r for r in recs if r.get("action") in ("BUY", "SELL")]
    actionable = [r for r in actionable if (r.get("rationale") or "").strip()]
    total_actionable = len(actionable)
    max_proposals = load_risk_limits().get("portfolio", {}).get("max_new_proposals_per_checkpoint")
    if max_proposals:
        actionable = sorted(actionable, key=lambda r: r["confidence"], reverse=True)[:max_proposals]

    if not actionable and not auto_results and not data_quality_alert:
        return

    lines = [
        f"{r['action']} {r['ticker']} (confidence {r['confidence']}) — {r.get('rationale', '')[:120]}"
        for r in actionable
    ]
    subject = f"[Bull-Trading] {checkpoint}: {len(actionable)} recommendation(s)"
    if max_proposals and total_actionable > len(actionable):
        subject += f" (top {len(actionable)} of {total_actionable})"
    body = "\n".join(lines) if lines else "No actionable recommendations this checkpoint."
    if max_proposals and total_actionable > len(actionable):
        body += (
            f"\n\n({total_actionable - len(actionable)} more actionable recommendation(s) this checkpoint, "
            "below the cap shown here — all saved to data/recommendations/, see `trading-agent approvals list` "
            "for the full set.)"
        )

    if data_quality_alert:
        body = f"** DATA QUALITY ALERT **\n{data_quality_alert}\n\n" + body
        subject = f"[Bull-Trading] {checkpoint}: DATA QUALITY ALERT"

    if auto_results:
        body += "\n\nAuto-applied:\n" + "\n".join(_auto_result_line(r) for r in auto_results)

    html_body = _digest_html(checkpoint, actionable, total_actionable, max_proposals, auto_results, data_quality_alert)

    if "email" in channels:
        try:
            from trading_agent.notify.senders import send_email

            send_email(subject, body, html_body)
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


def _digest_html(
    checkpoint: str,
    actionable: list[dict[str, Any]],
    total_actionable: int,
    max_proposals: int | None,
    auto_results: list[dict[str, Any]] | None,
    data_quality_alert: str | None,
) -> str:
    from trading_agent.notify import html as h

    inner = ""
    if data_quality_alert:
        inner += h.alert_banner(data_quality_alert)

    if actionable:
        inner += "".join(
            h.rec_row(r["ticker"], r["action"], r["confidence"], (r.get("rationale") or "")[:160])
            for r in actionable
        )
        if max_proposals and total_actionable > len(actionable):
            inner += h.muted(
                f"+{total_actionable - len(actionable)} more actionable this checkpoint, below the cap "
                "shown here — run `trading-agent approvals list` for the full set."
            )
    else:
        inner += h.muted("No actionable recommendations this checkpoint.")

    if auto_results:
        inner += h.section_heading("Auto-applied")
        for r in auto_results:
            if r["status"] == "submitted":
                inner += (
                    f'<div style="padding:6px 0;font-size:14px;">{h.chip("SUBMITTED", "positive")} '
                    f'<b>{r["ticker"]}</b> qty={r["qty"]}</div>'
                )
            else:
                inner += (
                    f'<div style="padding:6px 0;font-size:14px;">{h.chip(r["status"].upper(), "neutral")} '
                    f'<b>{r["ticker"]}</b> — {r.get("reason", "")[:120]}</div>'
                )

    subtitle = checkpoint.replace("_", " ").title()
    title = "Data Quality Alert" if data_quality_alert else f"{len(actionable)} Actionable"
    return h.wrap(title, subtitle, inner)


def _auto_result_line(result: dict[str, Any]) -> str:
    status = result["status"]
    if status == "submitted":
        return f"- {result['ticker']}: SUBMITTED qty={result['qty']}"
    return f"- {result['ticker']}: {status} — {result.get('reason', '')}"


def notify_daily_summary(summary: dict[str, Any]) -> None:
    """End-of-day learnings + benchmark comparison email/SMS (plan §12) — one
    per day, separate from notify_digest()'s per-checkpoint messages. `summary`
    is reporting.report_builder.build_daily_summary()'s return.

    Per user instruction 2026-09-30 ("should be detailed and capture
    everything done during the day by the agent" / "richer font n colors"):
    beyond the portfolio-vs-benchmark comparison and journaled findings this
    always had, the plain-text body now also covers the per-checkpoint
    BUY/SELL/HOLD breakdown, approvals (human vs. auto), every trade
    executed, and every auto-apply attempt regardless of outcome (not just
    successes) — see reporting.report_builder.build_daily_summary()'s
    docstring for where each of those fields comes from. The email also
    carries a richly styled HTML rendering (notify.html) alongside the same
    plain-text body senders.send_email() already sent; SMS is unchanged
    (a carrier gateway has no use for either the detail or the color).

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

    by_checkpoint = summary.get("by_checkpoint") or {}
    if by_checkpoint:
        lines.append("")
        lines.append("By checkpoint:")
        for cp, counts in by_checkpoint.items():
            lines.append(
                f"- {cp}: {counts['total']} scored "
                f"({counts['buy']} BUY / {counts['sell']} SELL / {counts['hold']} HOLD)"
            )

    approvals = summary.get("approvals") or {}
    if approvals.get("approved") or approvals.get("rejected"):
        lines.append("")
        lines.append(
            f"Approvals: {approvals.get('approved', 0)} approved "
            f"({approvals.get('auto', 0)} auto / {approvals.get('human', 0)} human), "
            f"{approvals.get('rejected', 0)} rejected"
        )

    trades = summary.get("trades") or []
    lines.append("")
    if trades:
        lines.append("Trades executed:")
        for t in trades:
            lines.append(f"- {t['ticker']} {t['side']} qty={t['qty']} [{t['source']}, {t['checkpoint']}]")
    else:
        lines.append("No trades executed today.")

    attempts = summary.get("auto_apply_attempts") or []
    if attempts:
        lines.append("")
        lines.append("Auto-apply attempts:")
        for a in attempts:
            if a["status"] == "submitted":
                lines.append(f"- {a['ticker']} [{a.get('checkpoint', '')}]: submitted qty={a.get('qty')}")
            else:
                lines.append(
                    f"- {a['ticker']} [{a.get('checkpoint', '')}]: {a['status']} — {a.get('reason', '')[:120]}"
                )

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

    lines.append("")
    lines.append("Learning & optimization:")
    for p in summary.get("optimization_proposals") or []:
        lines.append(f"- {p}")
    signal_hit_rates = summary.get("signal_hit_rates") or {}
    if signal_hit_rates:
        rates = ", ".join(f"{sig} {stats.get('hit_rate', 0):.0%}" for sig, stats in sorted(signal_hit_rates.items()))
        lines.append(f"  Per-signal hit rates so far: {rates}")
    learning_outcome = summary.get("learning_outcome")
    if learning_outcome:
        lines.append(f"  Possible outcome: {learning_outcome}")
    weights = summary.get("scoring_weights") or {}
    if weights:
        weight_str = ", ".join(f"{k} {v:g}" for k, v in weights.items())
        lines.append(f"  Weights currently in effect: {weight_str}")
    lines.append(
        "  Proposals are never applied automatically — incorporating one means a human "
        "edits config/agent_config.yaml and commits the change deliberately."
    )

    subject = f"[Bull-Trading] Daily summary — {summary['day']}"
    body = "\n".join(lines)
    html_body = _daily_summary_html(summary)

    if "email" in channels:
        try:
            from trading_agent.notify.senders import send_email

            send_email(subject, body, html_body)
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


def _daily_summary_html(summary: dict[str, Any]) -> str:
    from trading_agent.notify import html as h

    portfolio_pct = summary["portfolio_return_pct"]
    benchmark_pct = summary["benchmark_return_pct"]
    outperformance_pct = summary["outperformance_pct"]
    border = h.COLORS["border"]
    muted = h.COLORS["muted"]

    inner = ""
    inner += h.stat_card("Portfolio return", h.signed_pct(portfolio_pct))
    inner += h.stat_card(f"{summary['benchmark_symbol']} return", h.signed_pct(benchmark_pct))
    if outperformance_pct is not None:
        inner += h.stat_card(f"Vs. {summary['benchmark_symbol']}", h.signed_pct(outperformance_pct) + " pts")
    if portfolio_pct is None or benchmark_pct is None:
        inner += h.muted("One or both returns unavailable today — see the weekly report's P&L section instead.")

    inner += h.section_heading("Activity")
    inner += (
        f'<div style="font-size:14px;padding:4px 0;">'
        f"{summary['recommendations_count']} recommendations &middot; {summary['trades_count']} trades executed"
        f"</div>"
    )

    by_checkpoint = summary.get("by_checkpoint") or {}
    if by_checkpoint:
        inner += h.section_heading("By checkpoint")
        for cp, counts in by_checkpoint.items():
            buy_chip = h.chip(f"{counts['buy']} BUY", "positive")
            sell_chip = h.chip(f"{counts['sell']} SELL", "negative")
            hold_chip = h.chip(f"{counts['hold']} HOLD", "neutral")
            inner += (
                f'<div style="padding:8px 0;font-size:14px;border-bottom:1px solid {border};">'
                f'<b>{cp.replace("_", " ").title()}</b> — {counts["total"]} scored '
                f'<span style="margin-left:6px;">{buy_chip}</span> '
                f"<span>{sell_chip}</span> "
                f"<span>{hold_chip}</span>"
                f"</div>"
            )

    approvals = summary.get("approvals") or {}
    if approvals.get("approved") or approvals.get("rejected"):
        inner += h.section_heading("Approvals")
        approved_chip = h.chip(f"{approvals.get('approved', 0)} approved", "positive")
        rejected_chip = h.chip(f"{approvals.get('rejected', 0)} rejected", "negative")
        inner += (
            f'<div style="font-size:14px;padding:6px 0;">'
            f"{approved_chip} "
            f'<span style="color:{muted};">({approvals.get("auto", 0)} auto / '
            f'{approvals.get("human", 0)} human)</span> &nbsp; '
            f"{rejected_chip}"
            f"</div>"
        )

    trades = summary.get("trades") or []
    inner += h.section_heading("Trades executed")
    if trades:
        for t in trades:
            side_kind = "positive" if (t.get("side") or "").lower() == "buy" else "negative"
            inner += (
                f'<div style="padding:8px 0;font-size:14px;border-bottom:1px solid {border};">'
                f'{h.chip((t.get("side") or "").upper(), side_kind)} <b>{t.get("ticker")}</b> '
                f'qty={t.get("qty")} <span style="color:{muted};">'
                f'[{t.get("source")}, {t.get("checkpoint")}]</span></div>'
            )
    else:
        inner += h.muted("No trades executed today.")

    attempts = summary.get("auto_apply_attempts") or []
    if attempts:
        inner += h.section_heading("Auto-apply attempts")
        status_kind = {"submitted": "positive", "refused": "negative", "error": "negative", "skipped": "neutral"}
        for a in attempts:
            detail = f"qty={a.get('qty')}" if a["status"] == "submitted" else (a.get("reason") or "")[:120]
            inner += (
                f'<div style="padding:8px 0;font-size:14px;border-bottom:1px solid {border};">'
                f'{h.chip(a["status"].upper(), status_kind.get(a["status"], "neutral"))} '
                f'<b>{a["ticker"]}</b> <span style="color:{muted};">[{a.get("checkpoint", "")}]</span> '
                f"&mdash; {detail}</div>"
            )

    entries = summary["journal_entries"]
    inner += h.section_heading("Findings")
    if entries:
        for e in entries:
            outcome = e.get("outcome") or {}
            verdict = outcome.get("directionally_correct")
            verdict_kind = "positive" if verdict is True else ("negative" if verdict is False else "neutral")
            verdict_label = {"positive": "CORRECT", "negative": "WRONG", "neutral": "N/A"}[verdict_kind]
            inner += (
                f'<div style="padding:8px 0;font-size:14px;border-bottom:1px solid {border};">'
                f'{h.chip(verdict_label, verdict_kind)} <b>{e["ticker"]}</b> '
                f'<span style="color:{muted};">[{e["checkpoint"]}] {e["decision"]}</span>'
                f'<div style="margin-top:3px;">{e["reasoning"][:160]}</div></div>'
            )
    else:
        inner += h.muted("No journaled decisions today.")

    inner += h.section_heading("Learning &amp; Optimization")
    proposals = summary.get("optimization_proposals") or []
    if proposals:
        for p in proposals:
            inner += f'<div style="padding:6px 0;font-size:14px;">{h.chip("PROPOSAL", "neutral")} {p}</div>'
    else:
        inner += h.muted("No weight-adjustment proposal yet — not enough journaled history.")

    signal_hit_rates = summary.get("signal_hit_rates") or {}
    if signal_hit_rates:
        rate_chips = " ".join(
            h.chip(f"{sig} {stats.get('hit_rate', 0):.0%}", "neutral")
            for sig, stats in sorted(signal_hit_rates.items())
        )
        inner += f'<div style="padding:6px 0;font-size:14px;">{rate_chips}</div>'

    learning_outcome = summary.get("learning_outcome")
    if learning_outcome:
        inner += f'<div style="padding:6px 0;font-size:14px;"><b>Possible outcome:</b> {learning_outcome}</div>'

    weights = summary.get("scoring_weights") or {}
    if weights:
        weight_str = ", ".join(f"{k} {v:g}" for k, v in weights.items())
        inner += h.muted(f"Weights currently in effect: {weight_str}")

    inner += h.muted(
        "Proposals are never applied automatically — incorporating one means a human edits "
        "config/agent_config.yaml and commits the change deliberately."
    )

    return h.wrap("Daily Summary", summary["day"], inner)


def _markdown_to_html(text: str) -> str:
    """Just enough markdown for the weekly report's own fixed format —
    headings, bullets, **bold**, `code`, _italic_ lines and [links](url) —
    so its Apply/Dismiss links are clickable in the email. Text is escaped
    before any markup is added."""
    import re
    from html import escape

    from trading_agent.notify import html as h

    def inline(s: str) -> str:
        s = escape(s, quote=False)
        s = re.sub(
            r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
            lambda m: f'<a href="{escape(m.group(2), quote=True)}" style="color:{h.COLORS["buy"]};font-weight:700;">{m.group(1)}</a>',
            s,
        )
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`([^`]+)`", r'<code style="font-family:monospace;">\1</code>', s)
        return s

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("# "):
            continue  # the title already heads the email
        if stripped.startswith("## "):
            out.append(h.section_heading(escape(stripped[3:])))
        elif stripped.startswith("- "):
            indent = (len(line) - len(line.lstrip())) // 2
            out.append(
                f'<div style="font-size:14px;padding:3px 0 3px {12 + 16 * indent}px;">&bull; {inline(stripped[2:])}</div>'
            )
        elif stripped.startswith("_") and stripped.endswith("_"):
            out.append(h.muted(inline(stripped[1:-1])))
        else:
            out.append(f'<div style="font-size:14px;padding:4px 0;">{inline(stripped)}</div>')
    return "".join(out)


def notify_weekly_report(report_path: Path) -> None:
    """Emails/texts the weekly report once it's built
    (reporting.report_builder.build_weekly_report()'s return path) — that
    function only ever wrote a file; nothing sent it anywhere until this.

    The full markdown goes in the email body as-is — it's already
    human-readable plain text, nothing to reformat. SMS gets just the
    `## Summary` section (the report's fixed structure makes that a simple
    split), truncated to 300 chars like every other SMS this module sends;
    the full report is what the email is for.

    Not gated by a config flag of its own (unlike daily_summary_enabled) —
    whether it sends at all is already gated by notification_channels(),
    same as notify_digest(). A send failure is reported, not raised, same as
    every other notify_* function here.
    """
    channels = notification_channels()
    if not channels & {"email", "sms"}:
        return

    full_text = report_path.read_text()
    subject = f"[Bull-Trading] Weekly report — {report_path.stem}"

    if "email" in channels:
        try:
            from trading_agent.notify import html as h
            from trading_agent.notify.senders import send_email

            send_email(subject, full_text, h.wrap("Weekly Report", report_path.stem, _markdown_to_html(full_text)))
        except Exception as exc:  # noqa: BLE001 - a broken channel must not halt the routine
            print(f"NOTIFY (weekly report email) failed: {exc}")

    if "sms" in channels:
        try:
            from trading_agent.notify.senders import send_sms

            summary = full_text.split("## Summary", 1)[-1].split("##", 1)[0].strip()
            send_sms(f"Bull-Trading weekly report ({report_path.stem}):\n{summary}"[:300])
        except Exception as exc:  # noqa: BLE001
            print(f"NOTIFY (weekly report sms) failed: {exc}")


def optimization_link(option: dict[str, Any], intent: str) -> str | None:
    """Link to the Optimization Ticket page for one option, or None when
    notifications.optimization_ticket_artifact_url isn't configured.

    The fragment is short and plain — `#<option_id>.<intent>`, plus
    `.loosens` for an option that loosens a guardrail — because the viewer
    only passes a plain anchor through to the page: a first version that
    packed the whole option into the fragment (~900 chars) arrived empty.
    The page looks the option's details up in its own database (seeded by
    the Saturday routine) and asks for the acknowledgment whenever the
    fragment or those details say the option loosens a guardrail. Applying
    re-reads the option from data/optimizations/ regardless, so the fragment
    can't change what gets applied."""
    base = load_agent_config().get("notifications", {}).get("optimization_ticket_artifact_url")
    if not base:
        return None
    suffix = ".loosens" if option.get("loosens_guardrail") else ""
    return f"{base}#{option['id']}.{intent}{suffix}"


def _apply_command(option: dict[str, Any]) -> str:
    flag = " --acknowledge-loosening" if option.get("loosens_guardrail") else ""
    return f"trading-agent optimizations apply {option['id']}{flag}"


def _optimization_options_html(review: dict[str, Any]) -> str:
    from trading_agent.notify import html as h

    options = review.get("optimization_options") or []
    out = h.section_heading("Optimizations you can apply")
    if not options:
        return out + h.muted("No change cleared its minimum-evidence bar this time.")
    for option in options:
        diff = "<br>".join(
            f'<span style="font-family:monospace;">{c["path"]}: {c["from"]} &rarr; <b>{c["to"]}</b></span>'
            for c in option["changes"]
        )
        flag = h.chip("LOOSENS A GUARDRAIL", "negative") + " " if option.get("loosens_guardrail") else ""
        risk = (
            f'<div style="font-size:13px;color:{h.COLORS["alert"]};margin-top:4px;">{option["risk"]}</div>'
            if option.get("risk")
            else ""
        )
        apply_href, dismiss_href = optimization_link(option, "apply"), optimization_link(option, "dismiss")
        if apply_href and dismiss_href:
            apply_label = (
                "Review & apply (loosens a guardrail)" if option.get("loosens_guardrail") else "Apply this change"
            )
            actions = h.button(apply_label, apply_href) + h.button("Dismiss", dismiss_href, "negative")
        else:
            actions = h.muted(f"Apply with: {_apply_command(option)}")
        out += (
            f'<div style="border:1px solid {h.COLORS["border"]};border-radius:10px;padding:12px 14px;margin:8px 0;">'
            f'<div style="font-size:15px;font-weight:700;">{flag}{option["title"]}</div>'
            f'<div style="font-size:13px;margin-top:6px;">{diff}</div>'
            f'<div style="font-size:14px;margin-top:6px;">{option["why"]}</div>'
            f'<div style="font-size:13px;color:{h.COLORS["muted"]};margin-top:4px;">{option["effect"]}</div>'
            f"{risk}{actions}</div>"
        )
    return out + h.muted(
        "A click records your decision only. Options that loosen a guardrail ask you to confirm that "
        "on the next page. The next checkpoint run applies it within fixed limits and commits it to "
        "git, so it can be reverted."
    )


def _weekly_learning_review_html(review: dict[str, Any]) -> str:
    from trading_agent.notify import html as h

    border = h.COLORS["border"]
    muted = h.COLORS["muted"]

    inner = ""
    inner += h.stat_card("Trades executed", str(review["trades_count"]))
    inner += h.stat_card("Realized P&L (this week)", h.signed_dollar(review["realized_pnl"]["total"]))
    if review["unrealized_pnl"]["error"]:
        inner += h.muted(f"Unrealized (live positions): unavailable — {review['unrealized_pnl']['error']}")
    else:
        inner += h.stat_card("Unrealized P&L (live, now)", h.signed_dollar(review["unrealized_pnl"]["total"]))

    inner += _optimization_options_html(review)

    inner += h.section_heading("Portfolio status")
    status = review["portfolio_status"]
    if status["error"]:
        inner += h.muted(f"Unavailable — {status['error']}")
    else:
        inner += (
            f'<div style="font-size:14px;padding:4px 0;">'
            f"Equity ${status['equity']:,.2f} &middot; Cash ${status['cash']:,.2f} &middot; "
            f"Buying power ${status['buying_power']:,.2f} &middot; "
            f"{len(review['unrealized_pnl']['positions'])} open position(s)"
            f"</div>"
        )

    inner += h.section_heading("Orders not placed")
    never_chip = h.chip(f"{len(review['never_decided'])} never decided", "negative")
    approved_chip = h.chip(f"{len(review['approved_not_executed'])} human-approved, not executed", "negative")
    unlogged_chip = h.chip(f"{len(review.get('auto_outcome_unlogged') or [])} auto-apply, outcome not logged", "neutral")
    rejected_chip = h.chip(f"{len(review['rejected'])} human-rejected", "neutral")
    inner += f'<div style="padding:6px 0;">{never_chip} {approved_chip} {unlogged_chip} {rejected_chip}</div>'

    attempts_summary = review["auto_apply_attempts_by_status"]
    span = review.get("attempt_log_span") or ""
    span_note = f' <span style="color:{muted};">({span})</span>' if span else ""
    submitted_chip = h.chip(f"{attempts_summary.get('submitted', 0)} submitted", "positive")
    refused_chip = h.chip(f"{attempts_summary.get('refused', 0)} refused", "negative")
    skipped_chip = h.chip(f"{attempts_summary.get('skipped', 0)} skipped", "neutral")
    error_chip = h.chip(f"{attempts_summary.get('error', 0)} errored", "negative")
    inner += (
        f'<div style="padding:6px 0;">Auto-apply attempts{span_note}: '
        f"{submitted_chip} {refused_chip} {skipped_chip} {error_chip}</div>"
    )
    if review["refusal_breakdown"]:
        refusal_items = " ".join(
            h.chip(f"{label}: {count}", "negative")
            for label, count in sorted(review["refusal_breakdown"].items(), key=lambda kv: -kv[1])
        )
        inner += f'<div style="padding:6px 0;">{refusal_items}</div>'

    inner += h.section_heading("Opportunities lost or avoided")
    if review["missed_opportunities"]:
        for m in review["missed_opportunities"][:15]:
            verdict_kind = "negative" if m["would_have_helped"] else "neutral"
            verdict_label = "WOULD HAVE HELPED" if m["would_have_helped"] else "AVOIDED CORRECTLY"
            verdict_chip = h.chip(verdict_label, verdict_kind)
            action_badge = h.badge(m["action"])
            inner += (
                f'<div style="padding:8px 0;font-size:14px;border-bottom:1px solid {border};">'
                f"{verdict_chip} <b>{m['ticker']}</b> {action_badge} "
                f'<span style="color:{muted};">[{m["category"]}] conf {m["confidence"]}</span>'
                f'<div style="margin-top:3px;">{m["reference_price"]:.2f} &rarr; {m["current_price"]:.2f} '
                f"({m['pct_change']:+.2f}%)</div></div>"
            )
    else:
        inner += h.muted("No unexecuted actionable recommendations with a usable reference price this week.")

    inner += h.section_heading("Signal performance so far")
    if review["signal_hit_rates"]:
        rate_chips = " ".join(
            h.chip(f"{sig} {stats.get('hit_rate', 0):.0%}", "neutral")
            for sig, stats in sorted(review["signal_hit_rates"].items())
        )
        inner += f'<div style="padding:6px 0;">{rate_chips}</div>'
    else:
        inner += h.muted("Not enough outcome history yet.")

    inner += h.section_heading("Proposed model/config improvements")
    for p in review.get("optimization_proposals") or []:
        proposal_chip = h.chip("PROPOSAL", "neutral")
        inner += f'<div style="padding:6px 0;font-size:14px;">{proposal_chip} {p}</div>'
    if review.get("learning_outcome"):
        inner += f'<div style="padding:6px 0;font-size:14px;"><b>Possible outcome:</b> {review["learning_outcome"]}</div>'
    weights = review.get("scoring_weights") or {}
    if weights:
        weight_str = ", ".join(f"{k} {v:g}" for k, v in weights.items())
        inner += h.muted(f"Weights currently in effect: {weight_str}")

    notes = review.get("config_tuning_notes") or []
    if notes:
        inner += h.section_heading("Other observations for next week")
        for n in notes:
            inner += f'<div style="padding:6px 0;font-size:14px;">{n}</div>'

    inner += h.muted(
        "Nothing changes until you click Apply on an option above; the rest of this email is "
        "information only."
    )

    subtitle = f"last {review['lookback_days']} days ({review['window_start']} to {review['window_end']})"
    return h.wrap("Weekly Learning Review", subtitle, inner)


def notify_weekly_learning_review(review: dict[str, Any]) -> None:
    """Saturday weekly retrospective email/SMS (per user instruction
    2026-10-02, "evaluate all trades, portfolio status, trade orders not
    placed and opportunities lost ... feed and optimize for following week
    runs ... email should be sent out based on analysis, what
    recommendations were made and why"). `review` is
    reporting.report_builder.build_weekly_learning_review()'s return.

    Distinct from notify_weekly_report() (P&L + activity counts only, sent
    from the Friday pre_close trading-report routine, strict calendar week)
    — this is a separate Saturday-morning routine (see
    routines/weekly_learning_review.md) that looks specifically at what
    DIDN'T happen over a rolling `review['lookback_days']`-day window
    (30 by default, per user instruction 2026-10-02, "look broader ... to
    ensure analysis and predictions are more accurate" — wider than the
    weekly cadence this fires on so a single light week doesn't starve the
    patterns below of data): recommendations that expired undecided, were
    approved but never submitted, or were refused by a guardrail/auto-apply,
    plus a hindsight price check on each, and the same
    propose_weight_adjustments() proposals `trading-agent propose-weights`
    already surfaces.

    Per user instruction 2026-10-02 ("clickable optimization options for
    incorporating into the agent"), the review's `optimization_options`
    (optimizations.build_options()) lead the email, each with Apply/Dismiss
    links to the Optimization Ticket page (optimization_link()). A click
    only records the decision; the next checkpoint routine applies it via
    `trading-agent optimizations apply`, within optimizations.py's
    hard-coded bounds.

    Config-gated independently, same convention as daily_summary_enabled:
    notifications.weekly_learning_review_enabled (default true). A send
    failure is reported, not raised, same as every other notify_* function
    here.
    """
    if not load_agent_config().get("notifications", {}).get("weekly_learning_review_enabled", True):
        return
    channels = notification_channels()
    if not channels & {"email", "sms"}:
        return

    lines = [
        f"Bull-Trading weekly learning review — last {review['lookback_days']} days "
        f"({review['window_start']} to {review['window_end']})",
        "",
    ]

    options = review.get("optimization_options") or []
    lines.append("Optimizations you can apply:")
    if not options:
        lines.append("  None cleared their minimum-evidence bar this time.")
    for option in options:
        flag = " [LOOSENS A GUARDRAIL]" if option.get("loosens_guardrail") else ""
        lines.append(f"- {option['title']}{flag}")
        for c in option["changes"]:
            lines.append(f"    {c['path']}: {c['from']} -> {c['to']}")
        lines.append(f"    Why: {option['why']}")
        lines.append(f"    Effect: {option['effect']}")
        if option.get("risk"):
            lines.append(f"    Risk: {option['risk']}")
        apply_href = optimization_link(option, "apply")
        if apply_href:
            apply_label = "Review & apply (asks you to confirm)" if option.get("loosens_guardrail") else "Apply"
            lines.append(f"    {apply_label}: {apply_href}")
            lines.append(f"    Dismiss: {optimization_link(option, 'dismiss')}")
        else:
            lines.append(f"    Apply with: {_apply_command(option)}")
    if options:
        lines.append(
            "  A click records your decision only; the next checkpoint run applies it within fixed "
            "limits and commits it to git."
        )
    lines.append("")

    lines.append(
        f"Trades executed: {review['trades_count']} | Realized P&L: ${review['realized_pnl']['total']:,.2f}"
    )
    if review["realized_pnl"]["pending_fills"]:
        lines.append(f"  ({review['realized_pnl']['pending_fills']} order(s) with no confirmed fill yet)")
    if review["unrealized_pnl"]["error"]:
        lines.append(f"Unrealized (live positions): unavailable — {review['unrealized_pnl']['error']}")
    else:
        lines.append(f"Unrealized (live, open positions right now): ${review['unrealized_pnl']['total']:,.2f}")

    status = review["portfolio_status"]
    lines.append("")
    if status["error"]:
        lines.append(f"Portfolio status: unavailable — {status['error']}")
    else:
        lines.append(
            f"Portfolio status: equity ${status['equity']:,.2f}, cash ${status['cash']:,.2f}, "
            f"buying power ${status['buying_power']:,.2f}, "
            f"{len(review['unrealized_pnl']['positions'])} open position(s)"
        )

    lines.append("")
    lines.append(
        f"Orders not placed: {len(review['never_decided'])} never decided, "
        f"{len(review['approved_not_executed'])} human-approved but never submitted, "
        f"{len(review.get('auto_outcome_unlogged') or [])} auto-apply with no logged outcome, "
        f"{len(review['rejected'])} human-rejected"
    )
    attempts_summary = review["auto_apply_attempts_by_status"]
    span = review.get("attempt_log_span")
    span_suffix = f" ({span})" if span else ""
    lines.append(
        f"Auto-apply attempts{span_suffix}: {attempts_summary.get('submitted', 0)} submitted, "
        f"{attempts_summary.get('refused', 0)} refused, {attempts_summary.get('skipped', 0)} skipped, "
        f"{attempts_summary.get('error', 0)} errored"
    )
    if review["refusal_breakdown"]:
        lines.append(f"Refusal breakdown{span_suffix}:")
        for label, count in sorted(review["refusal_breakdown"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  - {label}: {count}")

    lines.append("")
    if review["missed_opportunities"]:
        lines.append("Opportunities lost or avoided (hindsight check):")
        for m in review["missed_opportunities"][:15]:
            verdict = "would have helped" if m["would_have_helped"] else "would NOT have helped"
            lines.append(
                f"- {m['ticker']} {m['action']} [{m['category']}] conf {m['confidence']}: "
                f"{m['reference_price']:.2f} -> {m['current_price']:.2f} ({m['pct_change']:+.2f}%) — {verdict}"
            )
    else:
        lines.append("No unexecuted actionable recommendations with a usable reference price this week.")

    lines.append("")
    lines.append("Signal performance so far:")
    if review["signal_hit_rates"]:
        for sig, stats in sorted(review["signal_hit_rates"].items()):
            lines.append(f"  - {sig}: {stats.get('hit_rate', 0):.0%} over {stats.get('n', 0)} call(s)")
    else:
        lines.append("  Not enough outcome history yet.")

    lines.append("")
    lines.append("Proposed model/config improvements (never applied automatically):")
    for p in review.get("optimization_proposals") or []:
        lines.append(f"- {p}")
    if review.get("learning_outcome"):
        lines.append(f"  Possible outcome if incorporated: {review['learning_outcome']}")
    weights = review.get("scoring_weights") or {}
    if weights:
        weight_str = ", ".join(f"{k} {v:g}" for k, v in weights.items())
        lines.append(f"  Weights currently in effect: {weight_str}")

    notes = review.get("config_tuning_notes") or []
    if notes:
        lines.append("")
        lines.append("Other observations for next week:")
        for n in notes:
            lines.append(f"- {n}")

    lines.append("")
    lines.append(
        "Nothing changes until you click Apply on an option above; the rest of this email is "
        "information only."
    )

    subject = (
        f"[Bull-Trading] Weekly learning review — last {review['lookback_days']} days "
        f"({review['window_start']} to {review['window_end']})"
    )
    if options:
        subject += f" — {len(options)} optimization(s) to review"
    body = "\n".join(lines)
    html_body = _weekly_learning_review_html(review)

    if "email" in channels:
        try:
            from trading_agent.notify.senders import send_email

            send_email(subject, body, html_body)
        except Exception as exc:  # noqa: BLE001 - a broken channel must not halt the routine
            print(f"NOTIFY (weekly learning review email) failed: {exc}")

    if "sms" in channels:
        try:
            from trading_agent.notify.senders import send_sms

            headline = (
                f"Bull-Trading weekly learning review ({review['window_start']} to {review['window_end']}): "
                f"{review['trades_count']} trades, {len(review['never_decided'])} never decided, "
                f"{len(review['approved_not_executed'])} approved-not-executed. Full report by email."
            )
            send_sms(headline[:300])
        except Exception as exc:  # noqa: BLE001
            print(f"NOTIFY (weekly learning review sms) failed: {exc}")


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


# The 4 scheduled checkpoints (plan §2) — duplicated from orchestrator.VALID_CHECKPOINTS
# rather than imported, since orchestrator already imports this module and a
# reverse import would be circular.
_CHECKPOINTS = ("pre_open", "market_open", "midday", "pre_close")


def pending_approvals_today() -> list[dict[str, Any]]:
    """Every actionable (BUY/SELL) recommendation from any of today's 4
    checkpoints that still has no approve/reject decision, whether or not its
    approval window has technically expired. A HOLD never needs a decision
    (no order would ever be submitted for it), so it's excluded here the same
    way notify_digest() excludes it, even though list_pending() itself
    doesn't discriminate by action.

    Each item carries an added "expired" bool (via is_expired() on the rec's
    own timestamp) so callers can separate "still within the approval window"
    from "past it, no longer approvable" without a second lookup. Expired
    ones are still returned, not dropped: by the time this runs (pre_close),
    a pre_open recommendation is routinely already past the default 2-hour
    window, and silently excluding it would make the daily reminder blind to
    the morning's misses — a human should see what they missed, not have it
    vanish.

    Deliberately not itself checkpoint-scoped: called once daily across all 4.
    """
    pending = []
    for checkpoint in _CHECKPOINTS:
        for rec in list_pending(checkpoint):
            if rec.get("action") not in ("BUY", "SELL"):
                continue
            pending.append({**rec, "expired": is_expired(rec["timestamp"])})
    return pending


def notify_pending_reminder() -> None:
    """Daily reminder for whatever's still awaiting a human decision —
    "email gets sent immediately" when a recommendation is first made is
    notify_digest()'s job (called once per checkpoint from
    orchestrator.run_checkpoint()); this is the follow-up for anything that
    immediate email didn't get a response to. Meant to be called once a day,
    from pre_close (trading-report's daily wrap-up), after all 4 checkpoints
    have had their chance.

    Silent when nothing is pending — this is a nudge for outstanding action,
    not a daily status ping, so an empty inbox stays empty. Splits the body
    into what's still actionable (within the approval window) and what
    expired today with no decision ever recorded (no longer approvable, kept
    visible so nothing silently disappears). A send failure is reported, not
    raised, same as notify_digest()/notify_daily_summary().
    """
    channels = notification_channels()
    if not channels & {"email", "sms"}:
        return

    pending = pending_approvals_today()
    if not pending:
        return

    still_actionable = [r for r in pending if not r["expired"]]
    expired = [r for r in pending if r["expired"]]

    def _line(r: dict[str, Any]) -> str:
        return (
            f"{r['ticker']} [{r['checkpoint']}]: {r['action']} (confidence {r['confidence']}) — "
            f"{r.get('rationale', '')[:120]}"
        )

    subject_bits = []
    if still_actionable:
        subject_bits.append(f"{len(still_actionable)} pending")
    if expired:
        subject_bits.append(f"{len(expired)} expired")
    subject = f"[Bull-Trading] {' / '.join(subject_bits)} — no decision recorded"

    body_parts = []
    if still_actionable:
        body_parts.append(
            f"{len(still_actionable)} recommendation(s) still awaiting a decision, within "
            "the approval window:\n\n" + "\n".join(_line(r) for r in still_actionable)
        )
    if expired:
        body_parts.append(
            f"{len(expired)} recommendation(s) expired today with no decision ever recorded — "
            "no longer approvable, shown for visibility only:\n\n"
            + "\n".join(_line(r) for r in expired)
        )
    body_parts.append("Run `trading-agent approvals list <checkpoint>` to review and decide.")
    body = "\n\n".join(body_parts)

    if "email" in channels:
        try:
            from trading_agent.notify.senders import send_email

            send_email(subject, body)
        except Exception as exc:  # noqa: BLE001 - a broken channel must not halt the routine
            print(f"NOTIFY (pending reminder email) failed: {exc}")

    if "sms" in channels:
        try:
            from trading_agent.notify.senders import send_sms

            sms_bits = []
            if still_actionable:
                sms_bits.append(f"{len(still_actionable)} pending")
            if expired:
                sms_bits.append(f"{len(expired)} expired")
            send_sms(f"Bull-Trading: {' / '.join(sms_bits)} — no decision recorded"[:300])
        except Exception as exc:  # noqa: BLE001
            print(f"NOTIFY (pending reminder sms) failed: {exc}")

"""Command-line entrypoint: trading-agent <command>.

Command groups map onto the plan's modules:
  ingest / backtest        -> data/market_data.py, backtest/engine.py (yfinance, historical)
  research / screen        -> research/perplexity_client.py
  checkpoint                -> orchestrator.py (research -> data -> score -> notify)
  approvals                 -> notify/approval_gateway.py (hard requirement: human in the loop)
  execute                   -> execute/order_manager.py (approval + kill-switch gated)
  report                    -> reporting/report_builder.py
  propose-weights           -> scoring/recommendation_engine.py (never auto-applies)
  chat                      -> agents/orchestrator.py (interactive Claude Agent SDK research)
"""

from __future__ import annotations

import argparse
import asyncio

from trading_agent.backtest.engine import backtest_moving_average
from trading_agent.config import load_watchlist
from trading_agent.data.market_data import fetch_watchlist, load_cached_price_history


def cmd_ingest(args: argparse.Namespace) -> None:
    tickers = args.tickers or load_watchlist()
    data = fetch_watchlist(tickers, period=args.period)
    for ticker, df in data.items():
        print(f"{ticker}: cached {len(df)} rows")


def cmd_backtest(args: argparse.Namespace) -> None:
    tickers = args.tickers or load_watchlist()
    for ticker in tickers:
        df = load_cached_price_history(ticker)
        print(f"{ticker}: {backtest_moving_average(df)}")


def cmd_chat(args: argparse.Namespace) -> None:
    from trading_agent.agents.orchestrator import run_research

    for message in asyncio.run(run_research(args.prompt)):
        print(message)


def cmd_research(args: argparse.Namespace) -> None:
    from trading_agent.research.perplexity_client import research_ticker

    print(research_ticker(args.ticker.upper(), args.checkpoint, force_refresh=args.refresh))


def cmd_screen(args: argparse.Namespace) -> None:
    from trading_agent.research.perplexity_client import screen_market

    print(screen_market())


def cmd_checkpoint(args: argparse.Namespace) -> None:
    from trading_agent.guardrails import RoutineHalted
    from trading_agent.orchestrator import run_checkpoint

    try:
        recs = run_checkpoint(args.name, extra_tickers=args.tickers)
    except RoutineHalted as exc:
        print(f"HALTED: {exc}")
        return
    for rec in recs:
        print(f"{rec['ticker']}: {rec['action']} (confidence {rec['confidence']})")


def cmd_approvals_list(args: argparse.Namespace) -> None:
    from trading_agent.notify.approval_gateway import list_pending

    for rec in list_pending(args.checkpoint):
        print(f"{rec['ticker']}: {rec['action']} (confidence {rec['confidence']}) — {rec.get('rationale', '')}")


def cmd_approvals_decide(args: argparse.Namespace) -> None:
    from trading_agent.notify.approval_gateway import record_decision

    terms = {"qty": args.qty} if args.qty else None
    record_decision(args.ticker.upper(), args.checkpoint, args.decision, terms)
    print(f"Recorded {args.decision} for {args.ticker.upper()} @ {args.checkpoint}")


def cmd_execute(args: argparse.Namespace) -> None:
    from trading_agent.execute.order_manager import OrderRefused, submit_approved_order

    recs = {r["ticker"]: r for r in _all_recs_today(args.checkpoint)}
    rec = recs.get(args.ticker.upper())
    if rec is None:
        print(f"No recommendation found for {args.ticker.upper()} @ {args.checkpoint} today.")
        return
    try:
        order = submit_approved_order(rec, args.qty)
        print(f"Submitted: {order}")
    except OrderRefused as exc:
        print(f"Refused: {exc}")


def _all_recs_today(checkpoint: str) -> list[dict]:
    from trading_agent.config import RECOMMENDATIONS_DIR
    from trading_agent.utils import load_json_list, today

    return load_json_list(RECOMMENDATIONS_DIR / today() / f"recs_{checkpoint}.json")


def cmd_journal_record(args: argparse.Namespace) -> None:
    from trading_agent.journal import record_entry

    entry = record_entry(
        args.ticker,
        args.checkpoint,
        decision=args.decision,
        reasoning=args.reasoning,
        action=args.action,
        confidence=args.confidence,
    )
    price = entry["reference_price"]
    print(
        f"Journaled {entry['decision']} for {entry['ticker']} @ {entry['checkpoint']}"
        + (f" (reference price {price:.2f})" if price else " (no quote available to measure from)")
    )


def cmd_journal_outcomes(args: argparse.Namespace) -> None:
    from trading_agent.journal import mark_outcomes

    updated = mark_outcomes(args.day)
    if not updated:
        print("No entries needed marking.")
        return
    for entry in updated:
        outcome = entry["outcome"]
        verdict = outcome["directionally_correct"]
        label = {True: "correct", False: "wrong", None: "no direction"}[verdict]
        print(f"{entry['ticker']}: {outcome['pct_change']:+.2f}% — {label}")


def cmd_journal_show(args: argparse.Namespace) -> None:
    from trading_agent.journal import load_entries

    entries = load_entries(args.day)
    if not entries:
        print("No journal entries.")
        return
    for entry in entries:
        outcome = entry.get("outcome")
        result = f"{outcome['pct_change']:+.2f}%" if outcome else "pending"
        print(
            f"{entry['ticker']} [{entry['checkpoint']}] {entry['decision']} "
            f"({result}) — {entry['reasoning']}"
        )


def cmd_journal_aggregate(args: argparse.Namespace) -> None:
    from trading_agent.journal import aggregate_performance

    metrics = aggregate_performance()
    n_tickers = len(metrics["by_ticker"])
    n_signals = len(metrics["by_signal_type"])
    if n_tickers == 0:
        print("No outcome-marked journal entries yet — nothing to aggregate.")
        return
    print(f"Aggregated {n_tickers} ticker(s), {n_signals} signal type(s) into data/performance/strategy_metrics.json")
    for signal, stats in sorted(metrics["by_signal_type"].items()):
        print(f"  {signal}: {stats['hit_rate']:.0%} over {stats['n']} call(s)")


def cmd_report(args: argparse.Namespace) -> None:
    from trading_agent.reporting.report_builder import build_daily_report, build_weekly_report

    if args.period == "daily":
        print(f"Wrote {build_daily_report(args.day)}")
    else:
        print(f"Wrote {build_weekly_report(args.week_start)}")


def cmd_propose_weights(args: argparse.Namespace) -> None:
    from trading_agent.scoring.recommendation_engine import propose_weight_adjustments

    for line in propose_weight_adjustments():
        print(f"- {line}")


def cmd_notify_test(args: argparse.Namespace) -> None:
    from trading_agent.notify.approval_gateway import notification_channels
    from trading_agent.notify.senders import send_email, send_sms

    channels = notification_channels()
    if "email" in channels:
        try:
            sent = send_email("[Bull-Trading] Test notification", "This is a test of the email channel.")
            print("email: sent" if sent else "email: skipped (NOTIFY_EMAIL_ADDRESS not set)")
        except Exception as exc:  # noqa: BLE001
            print(f"email: failed — {exc}")
    else:
        print("email: not in notifications.channel, skipped")

    if "sms" in channels:
        try:
            sent = send_sms("Bull-Trading: test notification")
            print("sms: sent" if sent else "sms: skipped (SMS_GATEWAY_ADDRESS not set)")
        except Exception as exc:  # noqa: BLE001
            print(f"sms: failed — {exc}")
    else:
        print("sms: not in notifications.channel, skipped")


def cmd_cron_status(args: argparse.Namespace) -> None:
    import datetime as dt

    from trading_agent.scheduling import all_checkpoint_crons_utc, is_dst_active

    on = dt.date.fromisoformat(args.date) if args.date else None
    label = "EDT (UTC-4)" if is_dst_active(on) else "EST (UTC-5)"
    print(f"US Eastern is {label} on {on or dt.datetime.now().date()}. Correct UTC cron expressions:")
    for name, cron in all_checkpoint_crons_utc(on).items():
        print(f"  {name}: {cron}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Fetch and cache long-history price data (yfinance) for backtesting.")
    ingest.add_argument("tickers", nargs="*")
    ingest.add_argument("--period", default="1y")
    ingest.set_defaults(func=cmd_ingest)

    backtest = sub.add_parser("backtest", help="Backtest the moving-average strategy on cached data.")
    backtest.add_argument("tickers", nargs="*")
    backtest.set_defaults(func=cmd_backtest)

    chat = sub.add_parser("chat", help="Interactive Claude Agent SDK research session.")
    chat.add_argument("prompt")
    chat.set_defaults(func=cmd_chat)

    research = sub.add_parser("research", help="Ad hoc Perplexity research for one ticker.")
    research.add_argument("ticker")
    research.add_argument("--checkpoint", default="adhoc")
    research.add_argument("--refresh", action="store_true", help="Bypass the 30-minute cache.")
    research.set_defaults(func=cmd_research)

    screen = sub.add_parser("screen", help="Broad Perplexity market screen for candidate tickers.")
    screen.set_defaults(func=cmd_screen)

    checkpoint = sub.add_parser("checkpoint", help="Run a full checkpoint: research -> data -> score -> notify.")
    checkpoint.add_argument("name", choices=["pre_open", "market_open", "midday", "pre_close"])
    checkpoint.add_argument("--tickers", nargs="*", help="Extra tickers beyond the watchlist.")
    checkpoint.set_defaults(func=cmd_checkpoint)

    approvals = sub.add_parser("approvals", help="Review and decide on pending recommendations.")
    approvals_sub = approvals.add_subparsers(dest="approvals_command", required=True)

    approvals_list = approvals_sub.add_parser("list", help="List recommendations awaiting a decision.")
    approvals_list.add_argument("checkpoint", choices=["pre_open", "market_open", "midday", "pre_close", "adhoc"])
    approvals_list.set_defaults(func=cmd_approvals_list)

    approvals_approve = approvals_sub.add_parser("approve")
    approvals_approve.add_argument("ticker")
    approvals_approve.add_argument("checkpoint")
    approvals_approve.add_argument("--qty", type=float)
    approvals_approve.set_defaults(func=cmd_approvals_decide, decision="approve")

    approvals_reject = approvals_sub.add_parser("reject")
    approvals_reject.add_argument("ticker")
    approvals_reject.add_argument("checkpoint")
    approvals_reject.add_argument("--qty", type=float)
    approvals_reject.set_defaults(func=cmd_approvals_decide, decision="reject")

    execute = sub.add_parser("execute", help="Submit an approved recommendation as a paper order (gated).")
    execute.add_argument("ticker")
    execute.add_argument("checkpoint")
    execute.add_argument("qty", type=float)
    execute.set_defaults(func=cmd_execute)

    notify_test = sub.add_parser(
        "notify-test", help="Fire a test message through every channel in notifications.channel."
    )
    notify_test.set_defaults(func=cmd_notify_test)

    journal = sub.add_parser("journal", help="Record decision reasoning and measure how calls turned out.")
    journal_sub = journal.add_subparsers(dest="journal_command", required=True)

    journal_record = journal_sub.add_parser("record", help="Journal why a decision was made.")
    journal_record.add_argument("ticker")
    journal_record.add_argument("checkpoint")
    journal_record.add_argument("--decision", required=True, choices=["approve", "reject"])
    journal_record.add_argument("--reasoning", required=True, help="The decision rationale, in your own words.")
    journal_record.add_argument("--action", choices=["BUY", "SELL", "HOLD"])
    journal_record.add_argument("--confidence", type=float)
    journal_record.set_defaults(func=cmd_journal_record)

    journal_outcomes = journal_sub.add_parser("outcomes", help="Measure unmarked entries against current prices.")
    journal_outcomes.add_argument("--day", help="YYYY-MM-DD, defaults to today.")
    journal_outcomes.set_defaults(func=cmd_journal_outcomes)

    journal_show = journal_sub.add_parser("show", help="List journal entries and their outcomes.")
    journal_show.add_argument("--day", help="YYYY-MM-DD, defaults to today.")
    journal_show.set_defaults(func=cmd_journal_show)

    journal_aggregate = journal_sub.add_parser(
        "aggregate",
        help="Roll all outcome-marked entries into data/performance/strategy_metrics.json.",
    )
    journal_aggregate.set_defaults(func=cmd_journal_aggregate)

    report = sub.add_parser("report", help="Build a daily or weekly markdown report.")
    report.add_argument("period", choices=["daily", "weekly"])
    report.add_argument("--day", help="YYYY-MM-DD, defaults to today.")
    report.add_argument("--week-start", help="YYYY-MM-DD Monday, defaults to this week.")
    report.set_defaults(func=cmd_report)

    propose = sub.add_parser("propose-weights", help="Print (never apply) proposed scoring-weight changes.")
    propose.set_defaults(func=cmd_propose_weights)

    cron_status = sub.add_parser(
        "cron-status",
        help="Print DST-correct UTC cron expressions for the four checkpoints (routine schedules are UTC-only).",
    )
    cron_status.add_argument("--date", help="YYYY-MM-DD (US/Eastern) to check, defaults to today.")
    cron_status.set_defaults(func=cmd_cron_status)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

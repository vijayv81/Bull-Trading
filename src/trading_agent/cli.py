"""Command-line entrypoint: trading-agent <command>."""

from __future__ import annotations

import argparse
import asyncio

from trading_agent.backtest.engine import backtest_moving_average
from trading_agent.config import WATCHLIST
from trading_agent.data.market_data import fetch_watchlist, load_cached_price_history
from trading_agent.strategy.recommendation import build_recommendation


def cmd_ingest(args: argparse.Namespace) -> None:
    tickers = args.tickers or WATCHLIST
    data = fetch_watchlist(tickers, period=args.period)
    for ticker, df in data.items():
        print(f"{ticker}: cached {len(df)} rows")


def cmd_recommend(args: argparse.Namespace) -> None:
    tickers = args.tickers or WATCHLIST
    for ticker in tickers:
        df = load_cached_price_history(ticker)
        print(build_recommendation(ticker, df))


def cmd_backtest(args: argparse.Namespace) -> None:
    tickers = args.tickers or WATCHLIST
    for ticker in tickers:
        df = load_cached_price_history(ticker)
        result = backtest_moving_average(df)
        print(f"{ticker}: {result}")


def cmd_research(args: argparse.Namespace) -> None:
    from trading_agent.agents.orchestrator import run_research

    messages = asyncio.run(run_research(args.prompt))
    for message in messages:
        print(message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Fetch and cache price history for the watchlist.")
    ingest.add_argument("tickers", nargs="*", help="Tickers to fetch (defaults to WATCHLIST).")
    ingest.add_argument("--period", default="1y")
    ingest.set_defaults(func=cmd_ingest)

    recommend = sub.add_parser("recommend", help="Compute a rule-based signal from cached data.")
    recommend.add_argument("tickers", nargs="*")
    recommend.set_defaults(func=cmd_recommend)

    backtest = sub.add_parser("backtest", help="Backtest the moving-average strategy on cached data.")
    backtest.add_argument("tickers", nargs="*")
    backtest.set_defaults(func=cmd_backtest)

    research = sub.add_parser("research", help="Run a Claude research agent prompt with trading tools.")
    research.add_argument("prompt", help="What to research, e.g. 'Analyze NVDA and recommend a signal'.")
    research.set_defaults(func=cmd_research)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

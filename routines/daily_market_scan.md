# Routine: daily_market_scan

A scheduled Claude Code routine (set up via the `/schedule` skill) that runs the
research agent over the watchlist and saves recommendations for later review.

## What it should do each run

1. `cd` into this project.
2. Run `trading-agent ingest` to refresh cached price data for the watchlist.
3. For each ticker, run `trading-agent research "Analyze <TICKER> using get_price_history
   and save a recommendation with save_recommendation, then summarize the signal and
   why it fired."`
4. Summarize the day's recommendations (buy/sell/hold + one-line rationale per ticker)
   back to the user — this is the message the routine reports on completion.

## Setting it up

Run `/schedule` and describe this routine, e.g.:

> Create a daily routine at 7am on weekdays that runs the daily_market_scan
> routine in `G:\My Drive\VSCode` (see routines/daily_market_scan.md) and reports
> the day's recommendations.

The skill will walk through cadence, working directory, and confirmation before
creating the cron schedule — nothing here runs automatically until you do that.

## Guardrails

- Research and signals only — the routine must never place or simulate trades.
- Every recommendation logged by `save_recommendation` should carry a rationale;
  treat a run that can't explain a signal as a failed run, not a silent skip.

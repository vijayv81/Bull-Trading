# Routines: the four daily checkpoints

Scheduled Claude Code routines (set up via the `/schedule` skill) that run the
research/scoring pipeline at each checkpoint and surface recommendations for
approval. **None of this executes a trade on its own** — see the hard
requirement in CLAUDE.md / plan §8.

| Checkpoint | Time (ET) | Emphasis |
|---|---|---|
| `pre_open` | 08:45 | Overnight news, earnings calendar, refresh watchlist |
| `market_open` | 09:40 | Opening-price vs. pre-market read, first-15-min momentum |
| `midday` | 12:30 | Re-scan movers, refresh research on unusual-volume names |
| `pre_close` | 15:45 | Final signal pass, next day's watchlist prep |

Each is the same pipeline (research → data → score → notify), just weighted
differently — see `src/trading_agent/orchestrator.py:run_checkpoint()`.

## What each run should do

1. `cd` into this project.
2. Run `trading-agent checkpoint <name>` (e.g. `trading-agent checkpoint pre_open`).
3. Report back the recommendations generated (ticker, action, confidence,
   rationale) — this is the routine's completion message.
4. Remind the user to review with `trading-agent approvals list <name>` and
   decide with `trading-agent approvals approve|reject <ticker> <name>` before
   anything can execute — an unreviewed recommendation simply expires
   (`approval_expiry_hours` in `config/risk_limits.yaml`).

At `pre_close`, also run `trading-agent report daily` and note that it should
be committed to git along with the day's `data/recommendations`, `data/approvals`,
and `data/trades` snapshots (plan §7 commit triggers).

## Setting it up

Run `/schedule` four times (once per checkpoint), e.g.:

> Create a weekday routine at 8:45am ET that runs `trading-agent checkpoint
> pre_open` in `G:\My Drive\VSCode\Bull-Trading` and reports the recommendations.

The skill walks through cadence, working directory, and confirmation before
creating each cron schedule — nothing here runs automatically until you do
that, and `config/risk_limits.yaml: operational.trading_enabled` stays `false`
until you deliberately flip it, so even an unattended run can't place an order.

## Guardrails

- Research and recommendations only, always human-approved before any paper
  order — see `execute/order_manager.py` and plan §8.
- Every recommendation must carry a rationale and sources; a run that can't
  produce one for a ticker should skip it, not guess.

---
name: trading-research
description: Runs a daily trading checkpoint — Perplexity research, scoring, and surfacing candidates for human approval. Use this whenever any of the four checkpoint routines fires (pre_open, market_open, midday, pre_close), whenever the user asks to research a ticker or "run a checkpoint", and whenever they ask what looks interesting today or want the watchlist re-scanned. Use it even for casual phrasings like "run pre_open", "anything good this morning?", or "check on TSLA" — this is the only sanctioned way to produce a recommendation in this project.
---

# Trading research checkpoint

Produces recommendations. Never approves or executes them — that is
`trading-trade`'s job, and the separation is the whole safety model.

## Before you start

Read `FEEDBACK.md` in this skill's directory. It holds corrections the user has
given during previous runs — emphasis they care about, tickers to treat
differently, phrasings of the summary they found useless. Those corrections are
the accumulated result of real runs, so they outrank the generic guidance below
when the two disagree.

## Run it

```bash
trading-agent checkpoint <pre_open|market_open|midday|pre_close>
```

One process, fresh state, everything read from disk — there is no session
memory to carry over from a previous checkpoint. If the user hasn't said which
checkpoint, infer it from the current ET time against the table in
`routines/trading_checkpoints.md` rather than asking.

Add `--tickers ABC DEF` when the user names specific symbols beyond the
watchlist.

## When the routine halts

`HALTED: Daily loss ... has reached the ...% cap` means the 2% daily loss
guardrail stopped the run before it spent any research budget. This is the
system working. Report the halt and stop — do not re-run with different
arguments, do not research the watchlist manually, and do not suggest editing
`config/risk_limits.yaml` to get past it. If the user wants to override a
guardrail that exists to protect them, that should be a deliberate decision they
reach on their own, not one you nudge them toward mid-run.

A halt reading `Cannot verify ... refusing to proceed blind` means Alpaca was
unreachable, not that money was lost. Say so plainly, since the remedy is
completely different — check credentials and connectivity, then re-run.

## Reporting back

The routine's completion message is the only thing the user may ever read, so
it has to stand on its own. For each recommendation give ticker, action,
confidence, and the one-line rationale — and lead with what changed since the
last checkpoint rather than restating the whole list, because a routine that
reports identical output four times a day trains the user to ignore it.

Then point them at the review step:

```bash
trading-agent approvals list <checkpoint>
```

Say explicitly that an unreviewed recommendation expires on its own
(`approval_expiry_hours`, default 2) rather than waiting around or
auto-executing. Users reasonably assume a missed notification means a pending
trade; here it means no trade.

## What not to do

A recommendation with no rationale and no sources should be dropped, not
patched with your own reasoning. The confidence score is built from research
the pipeline actually retrieved; substituting your own read of a ticker
produces a record that looks sourced but isn't, which is worse than a gap the
user can see.

Never edit `config/agent_config.yaml` scoring weights as part of a run. Weight
changes go through `trading-agent propose-weights` and a human commit — see
`trading-report`.

Nothing here is investment advice, and the summary shouldn't read like it is.
Report what the pipeline found; let the user decide what it means.

## Capturing feedback

When the user corrects you during a run — "stop including the movers", "I only
care about confidence above 70", "always show me the sources inline" — append
it to `FEEDBACK.md` before the run ends, in the format that file describes.
Corrections given mid-run are the most valuable signal available and they are
lost the moment the process exits, since each checkpoint starts cold.

Confirm what you recorded in one line so the user can catch a misread
immediately rather than discovering it three runs later.

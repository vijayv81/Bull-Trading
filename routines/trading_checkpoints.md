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

## Capabilities are skills

Each capability lives in `.claude/skills/` as a skill the routine invokes, rather
than as steps copy-pasted into four routine prompts that then drift apart:

| Skill | Capability |
|---|---|
| `trading-research` | Run the checkpoint, surface candidates for approval |
| `trading-trade` | Human approval, then gated paper execution |
| `trading-journal` | Record why a decision was made; measure how it turned out |
| `trading-report` | Daily/weekly reports, weight proposals, git snapshot |

Each skill reads its own `FEEDBACK.md` at the start of a run. That file holds
corrections given during previous runs, which is how the skills get refined
without editing their instructions by hand — see "Refining them" below.

## What each run should do

1. `cd` into this project.
2. **Invoke `trading-trade`'s "Sync email-link decisions first" step** even if
   nobody's asking for the approval flow this run — it's how a click on an
   emailed Approve/Reject link (routed through the Approval Ticket artifact)
   actually becomes a `data/approvals/` record. Skipping this because "no one
   asked for approvals" leaves clicked decisions stuck in the artifact's
   database indefinitely; an unattended run is exactly when nobody's around to
   trigger it manually.
3. **Invoke `trading-research`** and follow it. It runs the checkpoint, handles
   a guardrail halt, and reports the recommendations — that report is the
   routine's completion message.
4. If the user responds with decisions, **invoke `trading-trade`** for the
   approval flow, then **`trading-journal`** to capture their reasoning while
   it's fresh.

At `pre_close`, also invoke **`trading-journal`** to mark outcomes and
**`trading-report`** to build the daily report and commit the day's snapshot.

Invoke the skill rather than reaching for the CLI directly. The skills carry the
refusal handling, the "never decide for the user" rule, and the accumulated
feedback; a bare `trading-agent` call carries none of it.

## Refining them

When you correct the routine mid-run — "stop reporting the movers", "don't ask
me for qty under 5 shares" — the skill appends it to its `FEEDBACK.md` and
confirms what it recorded. Subsequent runs read it first, so the correction
sticks without anyone editing a `SKILL.md`.

Those files are tracked in git, so a refinement that turns out to be wrong shows
up in a diff and can be reverted. Worth skimming them occasionally: feedback
accumulates, and an entry that made sense in September may not in December.

## Setting it up

Run `/schedule` four times (once per checkpoint), e.g.:

> Create a weekday routine at 8:45am ET that runs `trading-agent checkpoint
> pre_open` in `G:\My Drive\VSCode\Bull-Trading` and reports the recommendations.

The skill walks through cadence, working directory, and confirmation before
creating each cron schedule — nothing here runs automatically until you do
that, and `config/risk_limits.yaml: operational.trading_enabled` stays `false`
until you deliberately flip it, so even an unattended run can't place an order.

## Cloud cron schedules are UTC-only — recheck at each DST transition

The four cloud routines (`bull-trading-pre-open/market-open/midday/pre-close`)
are cron-triggered in UTC; the trigger platform has no timezone field, so the
UTC time for a fixed ET checkpoint shifts by an hour across the twice-yearly
US DST transition unless the cron expression is recomputed and pushed.

`src/trading_agent/scheduling.py` is the single source of truth for the
correct UTC cron per checkpoint — it converts through `zoneinfo`
(`America/New_York`) rather than a hand-maintained transition calendar, so it
stays correct indefinitely. Run `trading-agent cron-status` to see today's
correct expressions, and near each transition (next: 2026-11-01, then
2027-03-14) an agent session with routine-management access updates the four
triggers' `cron_expression` (schedule-only, not the prompt) to match.

## Guardrails

- Research and recommendations only, always human-approved before any paper
  order — see `execute/order_manager.py` and plan §8.
- Every recommendation must carry a rationale and sources; a run that can't
  produce one for a ticker should skip it, not guess.

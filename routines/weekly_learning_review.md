# Routine: weekly learning review (Saturdays)

A fifth scheduled Claude Code routine, separate from the four weekday
checkpoints in `trading_checkpoints.md` and from the Friday `pre_close`
weekly report. Per user instruction 2026-10-02 ("set up a weekly learning
routine that will evaluate all trades, portfolio status, trade orders not
placed and opportunities lost. This should feed and optimize for following
week runs. Email should be sent out based on analysis, what recommendations
were made and why"): runs once a week, Saturday morning, when the market is
closed and nothing else is competing for the account.

## What it does

Everything here is read-only against the trading account — this routine
never submits an order and never writes `config/agent_config.yaml` or
`config/risk_limits.yaml`. It only reads the week's `data/` files and
Alpaca's current account state, and reports what it finds.

```bash
trading-agent weekly-learning-review                    # the week just finished
trading-agent weekly-learning-review --week-start YYYY-MM-DD
```

This builds `reports/learning/<year>-W<week>.md` and emails/SMS's it in one
step (`src/trading_agent/reporting/report_builder.py:build_weekly_learning_review()`,
`src/trading_agent/notify/approval_gateway.py:notify_weekly_learning_review()`),
covering:

- **Trades evaluated** — the week's realized P&L (confirmed fills) and a
  live unrealized P&L snapshot, same accounting `build_weekly_report()`
  already uses.
- **Portfolio status** — a live equity/cash/buying-power snapshot plus open
  position count, as of when the routine runs.
- **Trade orders not placed, and why** — every actionable (BUY/SELL)
  recommendation that did NOT result in a trade this week, split into: never
  decided (expired with no human response), approved but never submitted
  (a human approved it but never ran `trading-agent execute`), human-rejected,
  and auto-apply-refused (with a breakdown by which guardrail refused it).
- **Opportunities lost or avoided** — a hindsight price check on a sample of
  those unexecuted calls: what the ticker's price has actually done since
  vs. the recommendation's own `reference_price`. Deliberately two-sided —
  "would have helped" and "avoided correctly" are both reported, not just
  the cases that make skipping a trade look like a mistake.
- **Signal performance + weight proposals** — the same
  `recommendation_engine.propose_weight_adjustments()` output
  `trading-agent propose-weights` already prints, plus the scoring weights
  actually in effect right now, so "feed and optimize for following week
  runs" means exactly what it means everywhere else in this codebase (plan
  §6.3): surfaced for a human to read and decide on, never applied by this
  routine.
- **Other observations** — qualitative, data-grounded notes (e.g. "N of M
  refusals this week were stale-recommendation blocks") for human review,
  same "never applied automatically" rule as the proposals above.

Gated independently by `notifications.weekly_learning_review_enabled`
(default `true`) — `false` keeps every other notification (per-checkpoint
digests, the daily summary, the Friday weekly report) unaffected.

## What this run should do

1. **Get a push-authorized checkout first**, same reasoning as the four
   checkpoint routines — call `add_repo` for `vijayv81/bull-trading` with
   `access: "push"` and follow its instructions, then `register_repo_root`,
   before anything else.
2. Run `trading-agent weekly-learning-review` for the week just finished
   (no `--week-start` needed — it defaults to the most recent Monday, which
   on a Saturday is the week that just ended).
3. Report the findings in your completion message: trades, portfolio
   status, how many orders weren't placed and the top refusal reasons, the
   headline from the opportunities-lost hindsight check, and whether a
   weight-adjustment proposal was surfaced this week.
4. **Commit the week's report** — same convention as `trading-report`'s
   "Commit the snapshot" step:
   ```bash
   git add reports/learning/
   git status
   git commit -m "Weekly learning review <year>-W<week>: <n> trades, <n> never decided, <n> refused"
   ```
   **No human is present** (this is a scheduled routine) — push, open a PR
   against `main`, and merge it yourself, same as `trading-report`'s Friday
   weekly-report commit. If the push/PR/merge fails, say so plainly in the
   completion message rather than silently dropping it; the commit is still
   safe locally for a later run to pick up.

Invoke the `trading-report` skill for this — it already owns weekly
reporting and weight-proposal review, and this routine is a superset of
both, not a new capability. `trading-report/SKILL.md`'s "Send the weekly
report (Fridays only)" section has the sibling pattern for the Friday
report; this Saturday routine is the same shape, one day later, reading a
wider slice of the week's data.

## Setting it up

Create one Saturday-morning cron routine (via `/schedule` or the
Claude Code Remote `create_trigger` tool directly), e.g.:

> Create a weekly routine that fires every Saturday morning (US/Eastern) and
> runs `trading-agent weekly-learning-review` in a fresh checkout of
> `vijayv81/bull-trading`, then commits/pushes/merges the resulting report.

Same UTC-only caveat as the four checkpoints applies if the schedule is
pinned to a specific ET wall-clock time: recompute the cron expression
across the twice-yearly US DST transition (next: 2026-11-01, then
2027-03-14) if the trigger is defined in fixed UTC rather than a
timezone-aware cron string.

## Guardrails

- This routine never calls `execute.order_manager.submit_approved_order()`
  or writes an approval decision — it has no execution path at all, by
  design; it only reads and reports.
- Every proposal/observation it surfaces is for human review, same as
  `trading-agent propose-weights` — nothing here edits
  `config/agent_config.yaml` or `config/risk_limits.yaml`.

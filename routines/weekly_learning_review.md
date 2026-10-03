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
`config/risk_limits.yaml`. It only reads `data/` files and Alpaca's current
account state over a rolling lookback window, and reports what it finds.

```bash
trading-agent weekly-learning-review                          # last 30 rolling days ending today (build + email in one step; interactive use)
trading-agent weekly-learning-review --no-send                 # scheduled run, phase 1: build + snapshot, no email
trading-agent weekly-learning-review --send-saved --pr-url URL # scheduled run, phase 2: email the snapshot with the PR link
trading-agent weekly-learning-review --as-of YYYY-MM-DD        # last 30 rolling days ending there
trading-agent weekly-learning-review --lookback-days 14        # override the window size for one run
```

**Rolling 30-day window, not a calendar week**: per the same user's
follow-up instruction ("look broader into last 30 rolling days to ensure
analysis and predictions are more accurate"), this analyzes
`lookback_days` rolling calendar days ending at `as_of` (both default: 30
and today) — not the Monday-start 7-day week `trading-agent weekly-report`
uses. The routine still *fires* weekly; only the analysis window is wider,
so a single light week doesn't starve the refusal-category and
missed-opportunity patterns below of data. `lookback_days` defaults to
`agent_config.yaml -> reporting.weekly_learning_review_lookback_days` (30)
when the CLI flag isn't passed.

This builds `reports/learning/<window_end>.md` and emails/SMS's it in one
step (`src/trading_agent/reporting/report_builder.py:build_weekly_learning_review()`,
`src/trading_agent/notify/approval_gateway.py:notify_weekly_learning_review()`),
covering, over that window:

- **Trades evaluated** — realized P&L (confirmed fills) and a live
  unrealized P&L snapshot, same accounting `build_weekly_report()` already
  uses.
- **Portfolio status** — a live equity/cash/buying-power snapshot plus open
  position count, as of when the routine runs.
- **Trade orders not placed, and why** — every actionable (BUY/SELL)
  recommendation that did NOT result in a trade over the window, split
  into: never decided (expired with no human response), approved but never
  submitted (a human approved it but never ran `trading-agent execute`),
  human-rejected, and auto-apply-refused (with a breakdown by which
  guardrail refused it).
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
  refusals over the last 30 days were stale-recommendation blocks") for
  human review, same "never applied automatically" rule as the proposals
  above.

Gated independently by `notifications.weekly_learning_review_enabled`
(default `true`) — `false` keeps every other notification (per-checkpoint
digests, the daily summary, the Friday weekly report) unaffected.

## What this run should do

1. **Get a push-authorized checkout first**, same reasoning as the four
   checkpoint routines — call `add_repo` for `vijayv81/bull-trading` with
   `access: "push"` and follow its instructions, then `register_repo_root`,
   before anything else. If `add_repo` isn't available or fails, make the
   first line of the completion message "WEEKLY LEARNING REVIEW DID NOT
   RUN: <reason>" and stop — the first Saturday run (2026-10-03) ended
   silently after ~29s for exactly this kind of reason and nobody noticed
   until no email arrived.
2. **Build the report without emailing it:**
   ```bash
   trading-agent weekly-learning-review --no-send
   ```
   Defaults to the last 30 rolling days ending today. Writes
   `reports/learning/<window_end>.md`, saves the options to
   `data/optimizations/<date>/options.json`, and snapshots the review under
   `data/processed/` (gitignored) so step 6 emails exactly what gets
   committed rather than a recompute with different live quotes.
3. **Load this week's options into the Optimization Ticket page**, so a
   click on an email button opens a page that shows the full change. If
   `data/optimizations/<date>/options.json` exists and
   `notifications.optimization_ticket_artifact_url` is set, write each
   option to that page's database with one `ArtifactData` `batch`: per
   option, `{op: "set", collection: "options", doc_id: <option id>, data:
   <the option object>}`. This is display data only; applying always
   re-reads the option from the repo. If the write fails, say so and carry
   on.
4. **Commit and push the report** — same convention as `trading-report`'s
   "Commit the snapshot" step:
   ```bash
   git add reports/learning/ data/optimizations/
   git status
   git commit -m "Weekly learning review <window_end>: <n> trades, <n> never decided, <n> refused"
   ```
5. **Open a PR against `main` and do NOT merge it** (per user instruction
   2026-10-03: "pr should be included as clickable link in email with
   details so I can review and approve"). Unlike the weekday snapshot
   commits, the user reviews and merges this one. Give it a descriptive
   body: the window analyzed, the headline numbers, and each option (title,
   the exact config change, whether it loosens a guardrail). If the push or
   PR fails, say so plainly in the completion message and still go on to
   step 6 without a link — the email must go out either way.
6. **Email the review with the PR link:**
   ```bash
   trading-agent weekly-learning-review --send-saved --pr-url <the PR's URL>
   ```
   The email leads with a "Review & approve this report" button linking to
   the PR, what it contains, and what merging does. Merging matters: an
   option's Apply click can only be carried out by a later checkpoint once
   the options file is on `main`, so an unmerged PR leaves clicks pending.
7. Report the findings in your completion message: the window analyzed,
   trades, portfolio status, how many orders weren't placed and the top
   refusal reasons, the headline from the opportunities-lost hindsight
   check, whether a weight-adjustment proposal was surfaced, and the PR URL
   (stating that it is awaiting the user's merge).

Invoke the `trading-report` skill for this — it already owns weekly
reporting and weight-proposal review, and this routine is a superset of
both, not a new capability. `trading-report/SKILL.md`'s "Send the weekly
report (Fridays only)" section has the sibling pattern for the Friday
report; this Saturday routine is the same shape, one day later, reading a
rolling 30-day window rather than that report's strict calendar week.

## Setting it up

Create one Saturday-morning cron routine (via `/schedule` or the
Claude Code Remote `create_trigger` tool directly), e.g.:

> Create a weekly routine that fires every Saturday morning (US/Eastern) and
> runs the steps above in a fresh checkout of `vijayv81/bull-trading`:
> commits/pushes the report, opens a PR (never merges it), then emails the
> review with the PR link.

Same UTC-only caveat as the four checkpoints applies if the schedule is
pinned to a specific ET wall-clock time: recompute the cron expression
across the twice-yearly US DST transition (next: 2026-11-01, then
2027-03-14) if the trigger is defined in fixed UTC rather than a
timezone-aware cron string.

## Guardrails

- This routine never calls `execute.order_manager.submit_approved_order()`
  or writes an approval decision — it has no execution path at all, by
  design; it only reads and reports.
- This routine never edits `config/agent_config.yaml` or
  `config/risk_limits.yaml` itself. It writes the week's optimization
  options to `data/optimizations/<date>/options.json` and puts them in the
  email with Apply/Dismiss buttons. A change happens only after the user
  clicks Apply, when the next checkpoint's `trading-research` step "Apply
  clicked optimizations first" runs `trading-agent optimizations apply`
  within the bounds in `src/trading_agent/optimizations.py`. See CLAUDE.md,
  "One-click optimization options".

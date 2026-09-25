---
name: trading-report
description: Builds the daily and weekly markdown reports, surfaces proposed scoring-weight changes, and commits the day's audit snapshot to git. Use this at pre_close, whenever the user asks for a daily or weekly summary, when they ask how the week went, when they want the strategy weights reviewed, or when they ask you to commit the day's data. Use it even for loose phrasings like "wrap up the day" or "what happened this week?"
---

# Daily and weekly reporting

The close-out capability: turn the day's records into something readable, and
get it into git next to the data it describes.

## Before you start

Read `FEEDBACK.md` in this skill's directory — the user's standing preferences
on what belongs in a report and what's noise.

## Build the report

```bash
trading-agent report daily                    # today
trading-agent report daily --day YYYY-MM-DD
trading-agent report weekly                   # current week
trading-agent report weekly --week-start YYYY-MM-DD
```

Daily lands in `reports/daily/<date>.md`, weekly in
`reports/weekly/<year>-W<week>.md`.

## Send the daily summary

At `pre_close`, after `trading-journal` has marked outcomes and aggregated
performance, also send the end-of-day email/SMS:

```bash
trading-agent daily-summary                   # today
trading-agent daily-summary --day YYYY-MM-DD
```

This is separate from `report daily` above — it's the "learnings + how we did
vs. SPY today" email, not a markdown file. It's gated by
`notifications.daily_summary_enabled`; if the user's turned that off, running
it is a silent no-op, so check the config before telling them it didn't send.

## Say what the numbers aren't

The weekly report's `## P&L` section has two different kinds of number in it,
and they answer different questions — don't blur them together when relaying
it. **Realized P&L** is scoped to the week and only counts orders with a
confirmed fill; a pending-fill count means some of that week's trades aren't
in the total yet, not that the total is final. **Unrealized P&L** is a live
snapshot of open positions *as of right now*, not as of any day in the week —
don't present it as "how the week's positions are doing," since it also
reflects positions opened before the week started, or after it ended if you
run the report late.

The activity counts (recommendations, approvals, rejections, expiries, trades)
are still just activity, not performance — a summary that leads with "12
recommendations, 4 approved" invites the user to read it as performance on its
own, so pair it with the P&L section rather than reporting it alone.

The daily summary's portfolio-vs-SPY comparison is a third, different basis
again: equity-vs-prior-close for *both* legs, the same simple measure
`daily_loss_reason()` uses — not the weekly report's realized/unrealized P&L
accounting. Don't quote the two together as if they're the same number
measured twice; they answer "did today go well" and "what has this week's
trading actually made or lost," respectively.

The "Guardrail / Incident Notes" section in the daily report is currently always
empty — decision values are only ever `approve` or `reject`, so nothing can land
there. Don't present its emptiness as "no incidents"; it isn't evidence of
anything yet.

## Review proposed weight changes

```bash
trading-agent propose-weights
```

This only prints. It never writes `config/agent_config.yaml`, and neither should
you — a strategy change belongs in a commit the user made deliberately, with a
message saying why. Present the proposal, explain what it's inferring from, and
leave the decision with them.

Until the journal has real outcome history, expect this to report that there's
nothing to propose. That's correct, not broken.

## Commit the snapshot

The report and the data behind it should land in the same commit, so a report in
git history can always be traced to the records that produced it (plan §7).

```bash
git add reports/ data/recommendations/ data/approvals/ data/trades/ data/journal/
git status                                    # confirm before committing
git commit -m "Daily snapshot <date>: <n> recommendations, <n> approved"
```

Review `git status` output before committing rather than trusting a broad `git
add` — `data/raw/` and `data/processed/` are gitignored because they're bulky
and may contain raw API responses, and you want to notice immediately if
something unexpected is staged.

**A human is present in this conversation (interactive use):** ask before
pushing. Committing locally is easily undone; a push is visible to anyone
with access to the repo, and the user may want to look at the day's records
first.

**No human is present (a scheduled routine, per its prompt saying so
explicitly):** push, open a PR against `main`, and merge it yourself —
there's no one to ask, and a commit that only exists in this session's local
checkout or on an unreviewed branch nobody ever merges is indistinguishable
from the audit trail never having been written at all. This mirrors step 0's
`add_repo(access="push")` — that step exists specifically so this one can
complete unattended. Use a plain, factual PR title/body (e.g. "Daily
snapshot 2026-09-25: 6 recommendations, 2 approved") — this is data landing
in git, not a change under review. If the push, PR, or merge fails, say so
plainly in your summary rather than silently dropping it; the commit is
still safe locally for a later run or the user to pick up.

## Capturing feedback

When the user tells you what they actually want out of a report — "I don't care
about expiry counts", "always show me the week's rejects", "don't commit until I
look at it" — append it to `FEEDBACK.md` in the format that file describes, and
confirm what you recorded in one line.

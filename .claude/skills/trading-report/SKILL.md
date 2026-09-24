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

Ask before pushing. Committing locally is easily undone; a push is visible to
anyone with access to the repo, and the user may want to look at the day's
records first.

## Capturing feedback

When the user tells you what they actually want out of a report — "I don't care
about expiry counts", "always show me the week's rejects", "don't commit until I
look at it" — append it to `FEEDBACK.md` in the format that file describes, and
confirm what you recorded in one line.

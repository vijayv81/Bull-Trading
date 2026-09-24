---
name: trading-journal
description: Captures why a trading decision was made, in the user's own words, and later measures how it turned out. Use this immediately after any approve/reject decision, whenever the user explains their reasoning about a position, when they ask "how have my calls been doing?" or want to review past decisions, and at pre_close to mark outcomes. Use it even when they're just thinking out loud about why they like or dislike a setup — that reasoning is the input the scoring loop has no other way to get.
---

# Decision and outcome journal

`data/recommendations/` holds what the pipeline proposed and `data/approvals/`
holds what the user decided. Neither holds *why*, and nothing holds what the
price then did. This skill closes both gaps, and the `aggregate` step below
rolls the result into what `historical_hitrate()` and `propose-weights`
actually read — until there's enough journal history, those two still have
nothing to work from, but the pipe is no longer missing.

## Before you start

Read `FEEDBACK.md` in this skill's directory for how the user wants reasoning
captured and how much prompting they tolerate.

## Capture reasoning at decision time

Right after an approve/reject, record why — while it's still accurate:

```bash
trading-agent journal record <TICKER> <checkpoint> \
  --decision approve|reject \
  --reasoning "their actual words" \
  --action BUY --confidence 72
```

**Use their words, not your summary of them.** If the user says "the guidance
raise doesn't matter, I just like that it held 240 on three retests," record
that — not "technical support level confirmed." The whole value of this record
is being able to look back and see the reasoning that *actually* drove the
decision, including the parts that turned out to be superstition. A tidied-up
paraphrase reads better and teaches you nothing.

If their reasoning is thin ("gut feel", "seems fine"), record that verbatim too.
A journal where every entry sounds considered is a journal someone is
performing for, and it will quietly stop reflecting how decisions really get
made.

Ask once if they haven't said why. If they'd rather not explain, record the
decision with what you have and move on — nagging is how this becomes the step
they skip.

## Measure outcomes

At `pre_close`, or whenever the user asks how calls have gone:

```bash
trading-agent journal outcomes          # today
trading-agent journal outcomes --day YYYY-MM-DD
```

Re-runnable by design: entries already marked are left alone, so this can run at
every pre_close without double-counting. It skips entries whose reference price
couldn't be captured rather than inventing one.

To review:

```bash
trading-agent journal show --day YYYY-MM-DD
```

## Reporting outcomes honestly

When summarizing how calls went, resist smoothing. Report the rejected ones
that would have worked alongside the approved ones that did, because a journal
that only surfaces vindication is worse than none — it builds confidence in
exactly the reasoning that isn't earning it.

Do not extrapolate a hit rate from a handful of entries. A 2-for-3 day is
noise, and presenting it as a trend invites the user to change a strategy based
on nothing. Say how many entries it's based on, every time.

Never edit a past entry's `reasoning` once an outcome is known. If the user
wants to add a retrospective thought, that's a new entry — rewriting the stated
reason after seeing the result destroys the only thing this record is for.

## Roll outcomes into performance metrics

Right after marking outcomes, roll them up:

```bash
trading-agent journal aggregate
```

This recomputes `data/performance/strategy_metrics.json` from scratch out of
everything in `data/journal/` and `data/recommendations/` — a full recompute,
not an incremental update, so re-running it after fixing a bad entry just
works, and it never drifts from the source data. It's what `historical_hitrate()`
and `propose-weights` read; skip this step and both keep working off stale (or
empty) numbers no matter how much journal history piles up.

It's cheap and re-runnable, so there's no harm running it every time you run
`outcomes`, not just at pre_close.

Don't over-read a small `n`. The command reports how many calls each hit rate
is based on — say that number back to the user rather than just "62%", for the
same reason you never extrapolate a hit rate from a handful of entries above.

## Capturing feedback

When the user corrects how you journal — "stop asking me why on rejects", "mark
outcomes at open too, not just close" — append it to `FEEDBACK.md` in the
format that file describes, and confirm what you recorded in one line.

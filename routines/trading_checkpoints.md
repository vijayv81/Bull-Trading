# Routines: the four daily checkpoints, plus the 4:30pm post-close wrap-up

Scheduled Claude Code routines (set up via the `/schedule` skill) that run the
research/scoring pipeline at each checkpoint and surface recommendations for
approval. **By default none of this executes a trade on its own** — see the
hard requirement in CLAUDE.md / plan §8. The one exception is
`operational.auto_apply.enabled` in `config/risk_limits.yaml` — when the user
has turned it on, `run_checkpoint()` itself submits its highest-confidence
recommendations, up to a daily cap, through every existing guardrail. Check
that config before telling the user "nothing executes automatically" — say
what's actually configured instead. See CLAUDE.md's "Auto-apply" section
before touching any of that config.

| Checkpoint | Time (ET) | Emphasis |
|---|---|---|
| `pre_open` | 08:45 | Overnight news, earnings calendar, refresh watchlist |
| `market_open` | 09:40 | Opening-price vs. pre-market read, first-15-min momentum |
| `midday` | 12:30 | Re-scan movers, refresh research on unusual-volume names |
| `pre_close` | 15:45 | Final signal pass, next day's watchlist prep |

Each is the same pipeline (research → data → score → notify), just weighted
differently — see `src/trading_agent/orchestrator.py:run_checkpoint()`.

A fifth weekday routine, **`post_close` at 16:30 ET**, is not a checkpoint: it
researches nothing and proposes nothing. It is the end-of-day wrap-up, run
after the market closes so that the day's return, the marked outcomes and the
closing balance are final (see "The 4:30pm post_close wrap-up" below).

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

1. **Get a push-authorized checkout first.** A cloud-routine session starts
   with nothing checked out — call `add_repo` for `vijayv81/bull-trading` with
   `access: "push"` and follow its instructions (clone via the command it
   gives you, then `register_repo_root`), rather than a bare `git clone`. A
   bare clone has no push credentials, which is exactly what made a prior
   `pre_close` run's end-of-day commit fail to push ("repo not in this
   session's authorized repository set") — discovered only when it tried to
   commit, hours into the run. Do this even on a checkpoint that might not
   end up committing anything; it's cheap up front and expensive to discover
   missing at the end.
2. **Invoke `trading-trade`'s "Sync email-link decisions first" step** even if
   nobody's asking for the approval flow this run — it's how a click on an
   emailed Approve/Reject link (routed through the Approval Ticket artifact)
   actually becomes a `data/approvals/` record. Skipping this because "no one
   asked for approvals" leaves clicked decisions stuck in the artifact's
   database indefinitely; an unattended run is exactly when nobody's around to
   trigger it manually.
3. **Invoke `trading-research`** and follow it, starting with its "Apply
   clicked optimizations first" step: the weekly learning review email's
   Apply/Dismiss clicks only take effect when a checkpoint pulls them in.
   It then runs the checkpoint, handles
   a guardrail halt, and reports the recommendations — that report is the
   routine's completion message. If `auto_apply` is enabled, `run_checkpoint()`
   will also have already submitted its top candidates; the completion message
   must say what was auto-applied, not just what was proposed, or the user is
   reading a report of decisions that already happened as if they're still
   pending.
4. If the user responds with decisions on anything auto-apply didn't already
   take, **invoke `trading-trade`** for the approval flow, then
   **`trading-journal`** to capture their reasoning while it's fresh.

`pre_close` is only a checkpoint: steps 0–4 above, including the "Persist the
checkpoint's output" commit in `trading-research`. It no longer does the
end-of-day wrap-up — that moved to the 4:30pm `post_close` routine below.

Invoke the skill rather than reaching for the CLI directly. The skills carry the
refusal handling, the "never decide for the user" rule, and the accumulated
feedback; a bare `trading-agent` call carries none of it.

## The 4:30pm post_close wrap-up

Per user instruction 2026-10-08 ("move the daily summary to be generated after
market closes, so return and outcomes recorded are final for the day.
Schedule it at 4.30 pm et"). A `pre_close` run at 15:45 finishes around 15:55,
five minutes before the close, so everything it measured was intraday: the
day's return, the SPY comparison, the outcomes marked in `data/journal/`. The
`post_close` routine runs at 16:30 ET on weekdays and does, in this order:

1. **Get a push-authorized checkout** (same as step 1 above). It starts from a
   fresh clone of `main`, which already holds all four checkpoints' commits.
2. Invoke **`trading-journal`** to mark the day's outcomes (measured at the
   day's *closing price* once the session is over) and aggregate performance.
3. Invoke **`trading-report`** for the whole wrap-up, in its order: the daily
   report; `trading-agent optimizations propose` and, when it names an option,
   the PR that proposes it; `trading-agent daily-summary`; the pending-approvals
   reminder; the weekly report on Fridays (so the week's closing balance is the
   final Friday close); and finally the day's snapshot commit (PR, self-merged
   like every other snapshot).

No human is present; it never approves, rejects or sizes a trade, and it places
no orders (the checkpoints do that). If a step fails it says so plainly in its
completion message and carries on to the email, which must go out either way.
The trigger uses a `CRON_TZ=America/New_York` expression (`30 16 * * 1-5`), so
it follows DST by itself — same as all five cloud routines now (see "Cloud
cron schedules are DST-aware" below).

## Refining them

When you correct the routine mid-run — "stop reporting the movers", "don't ask
me for qty under 5 shares" — the skill appends it to its `FEEDBACK.md` and
confirms what it recorded. Subsequent runs read it first, so the correction
sticks without anyone editing a `SKILL.md`.

Those files are tracked in git, so a refinement that turns out to be wrong shows
up in a diff and can be reverted. Worth skimming them occasionally: feedback
accumulates, and an entry that made sense in September may not in December.

## Setting it up

Run `/schedule` four times (once per checkpoint) and once more for the
`post_close` wrap-up, e.g.:

> Create a weekday routine at 8:45am ET that runs `trading-agent checkpoint
> pre_open` in `G:\My Drive\VSCode\Bull-Trading` and reports the recommendations.

The skill walks through cadence, working directory, and confirmation before
creating each cron schedule — nothing here runs automatically until you do
that. Whether an unattended run can place an order depends entirely on
`config/risk_limits.yaml`: `operational.trading_enabled` is the master kill
switch (`false` blocks every order, auto or manual), and
`operational.auto_apply.enabled` separately controls whether the routine
submits its own top picks without a human approving them first — see
CLAUDE.md's "Auto-apply" section before enabling that combination.

## Cloud cron schedules are DST-aware (fixed 2026-10-30)

All five cloud routines — the four checkpoints plus `post_close` — now carry
a `CRON_TZ=America/New_York` prefix on `cron_expression` (e.g. `CRON_TZ=
America/New_York 45 8 * * 1-5` for `pre_open`), which the trigger platform
resolves against the real IANA tz database. That means DST is handled
automatically, permanently — confirmed live: switching `pre_open` to this
format on 2026-10-30 immediately computed the correct post-transition
`next_run_at` (2026-11-02T13:45:00Z, i.e. 8:45 ET under EST) with no manual
recomputation.

This replaces the original design, which scheduled the four checkpoints on
bare UTC cron (no timezone field) and needed `src/trading_agent/scheduling.py`
plus a pair of twice-yearly self-reminders to recompute and push each
transition by hand. Those reminders are now disabled (one had already fired
and auto-disabled itself; the other was disabled manually once the permanent
fix landed) — nothing should ever recreate that pattern for a cloud routine
again. `scheduling.py` / `trading-agent cron-status` are kept only as a
reference for what each checkpoint's UTC-equivalent time is on a given date;
they are no longer load-bearing for keeping any live trigger correct.

If you ever add a sixth cloud routine (or recreate one of these from
scratch), give it `CRON_TZ=America/New_York` from the start rather than a
bare UTC cron — there is no reason to reintroduce the manual-recheck
problem this section used to describe.

## Guardrails

- Every paper order goes through `execute/order_manager.py` (plan §8), which
  requires a matching approval record no matter who or what wrote it. By
  default that's always a human; `operational.auto_apply` is the one
  config-gated exception — see CLAUDE.md's "Auto-apply" section.
- Every recommendation must carry a rationale and sources; a run that can't
  produce one for a ticker should skip it, not guess.

# Bull-Trading

Automated daily-cadence trading research + paper-trading agent. Research via
Perplexity, execution/testing via Alpaca Paper Trading, file-based memory,
GitHub for versioning. Built from the implementation plan dated 2026-09-20
(the plan's section numbers, e.g. "§8", are referenced throughout the code).

**Not a trading bot.** It proposes; it never executes without a human
approval record on disk. Nothing here is investment advice.

## Two research paths, one data layer

1. **Automated pipeline** (`orchestrator.py` + `research/`, `scoring/`,
   `notify/`, `execute/`, `reporting/`) — cron-scheduled checkpoints that call
   Perplexity and Alpaca directly. This is the primary implementation of the
   plan.
2. **Interactive Claude agent** (`agents/`, Claude Agent SDK) — ad hoc
   `trading-agent chat "..."` sessions for exploratory research. Kept from
   the original scaffold; writes into the same `data/recommendations/` file
   layer via `notify.approval_gateway.save_recommendation()` so both paths
   are auditable from one place.

## Capabilities are skills

The four capabilities the scheduled routines use live in `.claude/skills/`, not
as steps duplicated across four routine prompts:

| Skill | Capability | Wraps |
|---|---|---|
| `trading-research` | Run a checkpoint, surface candidates | `trading-agent checkpoint` |
| `trading-trade` | Human approval, then gated execution | `trading-agent approvals` / `execute` |
| `trading-journal` | Record decision reasoning, measure outcomes | `trading-agent journal` |
| `trading-report` | Daily/weekly reports, weight proposals, snapshot commit | `trading-agent report` / `propose-weights` |

Each carries the refusal handling and the human-in-the-loop rules, so routines
invoke the skill rather than the CLI directly — a bare `trading-agent` call
carries none of that.

**Refinement loop:** each skill reads its own `FEEDBACK.md` at the start of a
run and appends corrections the user gives mid-run. Those entries outrank the
skill's generic guidance, and because they're tracked in git a bad refinement
shows up in a diff and can be reverted. Corrections that would loosen the
approval gate itself are explicitly *not* recorded this way — that's a reviewed
change to this file and the code, never a line in a feedback file that silently
changes how unattended runs behave.

## Layout

```
.claude/skills/          the four capability skills above, each with SKILL.md + FEEDBACK.md
config/                  watchlist.yaml, risk_limits.yaml (kill switch lives here), agent_config.yaml
src/trading_agent/
  config.py               YAML config + require_env() — the ONLY way credentials are read
  utils.py                shared date-partitioned file-layer helpers
  scheduling.py            DST-aware ET -> UTC cron conversion for the 4 checkpoints (`trading-agent cron-status`)
  orchestrator.py          run_checkpoint(): research -> data -> score -> notify
  research/                perplexity_client.py
  data/                    alpaca_client.py (primary), market_data.py (yfinance, backtest-only)
  journal.py              decision reasoning + outcome measurement + performance aggregation (plan §6.3)
  scoring/                 recommendation_engine.py — confidence formula (plan §6)
  notify/                  approval_gateway.py — hard requirement gate (plan §8); senders.py — email/SMS over Resend's HTTPS API (plan §12)
  execute/                 order_manager.py — approval + kill-switch gated Alpaca submission; auto_pilot.py — opt-in auto-apply (see below)
  reporting/               report_builder.py — daily/weekly markdown reports + realized/unrealized P&L,
                           plus the Saturday weekly learning review (see below)
  agents/                  interactive Claude Agent SDK research (see above)
  backtest/                engine.py — backtest_moving_average() (standalone SMA-crossover
                           comparison, from the original scaffold) + backtest_strategy()
                           (walks the real technical_score()/score_candidate() formula)
routines/                 trading_checkpoints.md — spec for the 4 scheduled routines (/schedule);
                          weekly_learning_review.md — spec for the 5th, Saturday-only routine
scripts/check_no_secrets.py   pre-commit credential scanner (plan §7.1 backstop)
data/                     raw+processed are gitignored; recommendations/approvals/trades/performance ARE tracked (audit trail)
```

## Credential policy (hard requirement, plan §7.1)

No secret is ever read from or written to a file — not `.env`, not any
`config/*.yaml`, not logs, not `data/`. Every client (`perplexity_client.py`,
`alpaca_client.py`) calls `config.require_env("SOME_VAR")`, which reads
straight from the process environment and fails fast if unset. Config files
may name the *variable*, never the value (see `config/agent_config.yaml ->
credentials`). There is **no `.env.example`** in this project by design —
that's a deliberate change from the original scaffold once this plan's
credential policy was implemented.

Required env vars: `PERPLEXITY_API_KEY`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`.
(`GITHUB_TOKEN` is named in config for a future git-push helper; nothing
currently reads it.)

Optional, only needed when `config/agent_config.yaml -> notifications.channel`
includes `email` or `sms` (`notify/senders.py`, plan §12): `RESEND_API_KEY` —
required the same fail-fast way as the three above once that channel is
enabled. Sent over Resend's HTTPS API deliberately, not SMTP: this project
runs in a cloud sandbox whose network only proxies HTTPS egress, and a raw
SMTP socket (port 587/465) times out at connect() there — confirmed directly,
not a theoretical concern. `RESEND_FROM_ADDRESS` is optional (defaults to
Resend's no-setup sandbox sender, `onboarding@resend.dev`) — but that sandbox
sender can only send to the *account's own verified email* (confirmed live:
`"You can only send testing emails to your own email address... verify a
domain to send to other recipients"`), which is why `channel` ships without
`sms` by default: a carrier's SMS gateway address is never that address. Once
`RESEND_FROM_ADDRESS` is on a domain verified at resend.com/domains, add
`sms` back — nothing else changes. `NOTIFY_EMAIL_ADDRESS` (the email destination) and
`SMS_GATEWAY_ADDRESS` (a phone's carrier email-to-SMS address, e.g.
`<number>@tmomail.net`) are destinations, not secrets, but get the same
treatment — never in a file — and are soft-optional: unset simply means that
channel silently sends nothing rather than failing. Test the whole path with
`trading-agent notify-test`.

`trading-agent daily-summary` (gated by `notifications.daily_summary_enabled`,
default `true`) sends one end-of-day email/SMS, separate from the
per-checkpoint digests: today's journaled reasoning + outcomes as "findings,"
and the account's own return today (`reporting/report_builder.py`'s
`_portfolio_return_pct()`, the same equity-vs-prior-close measure
`daily_loss_reason()` uses) against `BENCHMARK_SYMBOL` (`"SPY"` by default)
over the same window. Either return being unavailable (Alpaca unreachable, or
insufficient benchmark bars) is reported as unavailable, never guessed at or
silently shown as 0%.

**Detailed and richly styled**, per user instruction 2026-09-30 ("should be
detailed and capture everything done during the day ... richer font n
colors"): `build_daily_summary()` now also includes a per-checkpoint
BUY/SELL/HOLD breakdown, approvals split human vs. auto, every trade
executed, and — new — **every auto-apply attempt regardless of outcome**,
not just the successes `data/trades/<date>/orders_submitted.json` already
recorded. `execute/auto_pilot.py:_persist_attempt()` writes each candidate's
submitted/refused/skipped/error result to
`data/trades/<date>/auto_apply_attempts_<checkpoint>.json` as it happens —
previously that information only ever lived in `auto_apply()`'s return
value for that one checkpoint's `notify_digest()` call, then was gone, so a
day of nothing-but-refusals was indistinguishable after the fact from a day
nothing was attempted. The email itself gets a richly styled HTML rendering
(`notify/html.py`'s `stat_card`/`chip`/`signed_pct` helpers — colored
positive/negative figures, section headers, status pills) alongside the
same (now longer) plain-text body; SMS is unchanged, a carrier gateway has
no use for either the extra detail or the color.

**Learning & optimization**, per user instruction 2026-10-01 ("I want email
to include what optimization or learning occurred during the day and if
they were incorporated and also possible outcomes with this learning"):
`build_daily_summary()` (`reporting/report_builder.py`) now also surfaces
`recommendation_engine.propose_weight_adjustments()`'s current output
(`optimization_proposals`), the per-signal hit rates it's computed from
(`_signal_type_hit_rates()`, reading `data/performance/strategy_metrics.json
-> by_signal_type`), the scoring weights actually in effect right now
(`load_agent_config()['scoring_weights']`), and a new qualitative
`learning_outcome` note (`_learning_outcome_note()`) describing what
incorporating the proposal would plausibly change — deliberately framed as a
directional, non-numeric description ("would lean more on X and less on
Y"), never a projected return, consistent with this project's "not
investment advice" line at the top of this file. Because
`propose_weight_adjustments()` **never writes config itself** (see "What's
not built yet" / the plan §6.3 rule it's named after), "incorporated" can
only ever mean a human read the proposal and deliberately edited
`config/agent_config.yaml -> scoring_weights` — so rather than claim a
proposal was or wasn't incorporated, the email instead shows the weights
*currently in effect* side by side with the proposal, plus an explicit line
every time: proposals are never applied automatically. No proposal yet
(too little journaled history for `propose_weight_adjustments()` to say
anything) reports exactly that, not a fabricated finding.

`trading-agent weekly-report` builds `reporting/report_builder.py`'s weekly
markdown rollup (`reports/weekly/<year>-W<week>.md`) *and* emails/SMS it —
`build_weekly_report()` itself only ever wrote the file; nothing sent it
anywhere before `notify_weekly_report()`. No separate scheduled trigger for
this: the `trading-report` skill runs it from `pre_close` on Fridays only
(the last checkpoint of the trading week), reusing the existing Mon-Fri
`pre_close` schedule rather than adding a new automation object for a
once-a-week job.

## Human-approval notifications (plan §8/§12)

Two, deliberately different in cadence:

1. **Immediate, per checkpoint** — `notify_digest()` (`notify/approval_gateway.py`),
   called from `orchestrator.run_checkpoint()` right after scoring. Every time
   a checkpoint produces an actionable (BUY/SELL) recommendation, the
   configured email/SMS channel gets it the same run — there's no delay
   between "a recommendation needing a decision exists" and "a human is told."
2. **Daily reminder for anything still undecided** — `trading-agent approvals
   remind` (`notify.approval_gateway.notify_pending_reminder()` /
   `pending_approvals_today()`), run once from `pre_close`'s `trading-report`
   wrap-up. Sweeps all 4 checkpoints' recommendations for the day, not just
   `pre_close`'s own, and reports two groups: still within the approval
   window (genuinely actionable right now) and expired with no decision ever
   recorded (no longer approvable — shown so nothing silently vanishes from
   view rather than filtered out, since the default 2-hour window means a
   `pre_open` recommendation is routinely already expired by the time this
   runs). Silent (sends nothing) when nothing's outstanding — this is a nudge,
   not a daily status ping.

`notify_digest()` also caps how many actionable recommendations it *headlines*
at `risk_limits.yaml -> portfolio.max_new_proposals_per_checkpoint`, ranked by
confidence, so a mover-heavy checkpoint surfacing a dozen actionable calls
doesn't read like a dozen equally-urgent texts (the subject line notes
`(top N of total)`). This is a display cap only — every recommendation is
still written to `data/recommendations/` regardless; unset/zero means no cap.

**Crisp and actionable-only**, per user instruction 2026-09-30: `notify_digest()`
now only ever shows actionable (BUY/SELL) items, drops any recommendation with
a blank `rationale` (a research call that technically succeeded but produced
no usable summary — "no analysis info" is skipped, not shown as an empty
entry), and never mentions `failed_tickers` (research errors — not actionable,
already visible in that checkpoint session's own console output) in the
email/SMS body at all. A checkpoint with nothing actionable, nothing
auto-applied, and no data-quality alert now sends **nothing**, on either
channel — same "silence is fine on a quiet run" convention
`notify_pending_reminder()` already used. The email also carries a
mobile-friendly HTML rendering (`notify/html.py` — inline styles only, no
external CSS/webfonts, so it renders consistently in Gmail/Apple Mail on a
phone) alongside the plain-text body `senders.send_email()` already sent;
plain text remains the fallback for clients that strip HTML.

## Weekly learning review (new routine, Saturdays)

Per user instruction 2026-10-02 ("set up a weekly learning routine that
will evaluate all trades, portfolio status, trade orders not placed and
opportunities lost. This should feed and optimize for following week runs.
Email should be sent out based on analysis, what recommendations were made
and why. Set this job to run every Saturday 6am"): a fifth scheduled
routine — see `routines/weekly_learning_review.md` for the full spec and
how it's set up — distinct from the four weekday checkpoints and from the
Friday `pre_close` weekly report, firing once a week on Saturday morning
when the market's closed.

**Rolling 30-day analysis window**, per (the same day's) follow-up user
instruction ("look broader into last 30 rolling days to ensure analysis and
predictions are more accurate"): the routine still *fires* weekly
(Saturdays), but `build_weekly_learning_review(as_of, lookback_days)`
analyzes `lookback_days` rolling calendar days ending at `as_of` (default:
today) — not the 7-day Monday-start calendar week `build_weekly_report()`
uses. `lookback_days` defaults to `agent_config.yaml -> reporting.
weekly_learning_review_lookback_days` (30) when not passed explicitly, via
the new `reporting:` top-level config section (distinct from `scoring_weights`/
`notifications` — these are report-shape parameters, not scoring or risk
ones). The point of widening it: a single light week was too thin a sample
for the refusal-category and missed-opportunity patterns below to mean
much — 30 rolling days gives them more to work with without changing how
often the email actually goes out. `trading-agent weekly-learning-review
--as-of YYYY-MM-DD --lookback-days N` overrides either for an ad hoc run;
the review's own `window_start`/`window_end`/`lookback_days` fields (and
the email subject/report header) always say exactly what window produced
the numbers, so this is never silently inconsistent with a shorter/longer
run.

`reporting/report_builder.py:build_weekly_learning_review()` /
`notify/approval_gateway.py:notify_weekly_learning_review()`
(`trading-agent weekly-learning-review`) cover, over that window:

- **Trades evaluated** — realized P&L (confirmed fills) over the window and
  a live unrealized P&L snapshot, reusing `_realized_pnl()`/`_unrealized_pnl()`
  from `build_weekly_report()`.
- **Portfolio status** — a live equity/cash/buying-power snapshot plus open
  position count, as of when the routine runs.
- **Trade orders not placed, and why** — unlike every other report here,
  this one also looks at actionable (BUY/SELL) recommendations with NO
  trade behind them at all, split by why: never decided (expired with no
  human response — these never even reached `data/journal/`, so the
  improvement loop never learned anything from them), approved but never
  submitted (a human recorded an approval but never ran `trading-agent
  execute` — approval and execution are deliberately separate steps, see
  `.claude/skills/trading-trade`), human-rejected, and auto-apply-refused
  (`execute/auto_pilot.py`'s persisted attempt log), broken down by which
  guardrail refused it (`_categorize_refusal()` matches each attempt's
  refusal string against the fixed phrases `guardrails.py` actually raises).
- **Opportunities lost or avoided** — a hindsight price check
  (`_missed_opportunity_counterfactuals()`) on a sample of those unexecuted
  calls (deduped by ticker, highest confidence first, capped at
  `MAX_MISSED_OPPORTUNITY_LOOKUPS` live quote lookups): current price vs.
  the recommendation's own `reference_price`, reusing the same comparison
  `journal.mark_outcomes()` does for a decided-and-journaled call.
  Deliberately two-sided — `would_have_helped` is reported both ways, not
  just the cases that make skipping a trade look like a mistake, same
  honesty the earlier 2026-09 stale-recommendation counterfactual chat
  analysis insisted on (a modest, non-uniform result, not every blocked
  trade a missed win). A candidate with no reference price or no live quote
  is excluded, not guessed at — same "exclude, don't fake" convention as
  every scoring component in this project.
- **Signal performance + weight proposals** — the exact same
  `recommendation_engine.propose_weight_adjustments()` output
  `trading-agent propose-weights` already prints, plus the scoring weights
  actually in effect right now. "Feed and optimize for following week runs"
  means exactly what it means everywhere else in this codebase (plan
  §6.3): surfaced for a human to read and decide on. **This routine never
  writes `config/agent_config.yaml` or `config/risk_limits.yaml`** — same
  hard rule as every other proposal mechanism here.
- **Other observations** (`_config_tuning_notes()`) — qualitative,
  data-grounded notes citing the window's actual counts (e.g. "N of M
  auto-apply refusals over the last 30 days were stale-recommendation
  blocks — current `execution.max_price_drift_pct` is X%"), never a config
  change applied by this function.

Writes `reports/learning/<window_end>.md` (tracked in git, same as
`reports/weekly/` and `reports/daily/` — dated by the window's end rather
than an ISO week number, since the window is no longer Monday-aligned) and
emails/SMS's it in one step, with a styled HTML rendering alongside the
plain-text body, same convention as the daily summary. Gated independently
by `notifications.weekly_learning_review_enabled` (default `true`) —
`false` keeps every other notification unaffected, no code change needed.

## Approval + execution (hard requirement, plan §8)

`execute/order_manager.py:submit_approved_order()` is the only sanctioned
path to an Alpaca order in this codebase. It refuses unless, in order:
1. `config/risk_limits.yaml -> operational.trading_enabled` is `true` (kill switch).
2. A `data/approvals/<date>/decisions_<checkpoint>.json` record exists for
   that ticker+checkpoint with `decision: approve`.
3. That approval hasn't expired (`approval_expiry_hours`).
4. The requested qty matches the approved qty within 1%.

`allow_live_trading` in the same file is a second, independent guard —
`data/alpaca_client.py:trading_client()` refuses to even construct a client
if it's `true`; flipping it is deliberately not sufficient on its own.

## Auto-apply (opt-in exception to the human-approval requirement)

**Read this before changing anything below it.** The line at the top of this
file — "it never executes without a human approval record on disk" — has one
deliberate, config-gated exception: `execute/auto_pilot.py:auto_apply()`,
wired into `orchestrator.run_checkpoint()`. It exists because a paper-trading
account was explicitly set up to run unattended; it does not relax anything
about how an order actually gets checked, and every toggle below is a
one-line revert with no code change.

**How it stays honest about what it is:**
- Off unless `risk_limits.yaml -> operational.auto_apply.enabled` is `true`.
  `false` is the complete revert — `run_checkpoint()` then behaves exactly as
  the rest of this document describes, unconditionally.
- When on, it picks the highest-confidence actionable (BUY/SELL) recommendations
  from *this* checkpoint, up to `max_trades_per_day` **shared across all 4
  checkpoints for the day** (re-read from `data/trades/` fresh each call, not
  reset per checkpoint) — 5 by default.
- It writes its own `data/approvals/` decision — tagged `terms.source: "auto"`
  — then calls the exact same `submit_approved_order()` a human's approval
  would. Every guardrail still runs: `trading_enabled`, the options ban, the
  5% position cap, the no-short-sale rule, the daily-loss halt. This module
  adds no new bypass; it only automates who clicks approve.
- Every submitted order is tagged `source: "auto"` in `data/trades/`, next to
  `"human"` for everything else — the audit trail never blurs the two.
  `list_pending()` correctly stops showing a ticker once auto-applied, same
  as it would for a human decision, because it *is* a decision, just not a
  human's.
- Qty comes from `suggested_size_pct_of_portfolio` (see below), converted to
  whole shares at the current quote — never rounds up. For a SELL, that qty
  is additionally capped at what's actually held
  (`auto_pilot._held_qty()`) — `suggested_size_pct_of_portfolio` is a
  fresh-position-sizing target with no idea how much of the ticker exists,
  so uncapped it almost always exceeded a real holding and got refused by
  `short_sale_reason()` as an attempted short. That silently defeated the
  continuous-position-monitoring stop-loss/take-profit SELLs below —
  confirmed live 2026-09-28, where GRMLW/ABLVW/APUS all correctly scored
  SELL on a stop-loss breach and every auto-apply attempt on them was
  refused, so the losses just kept compounding. A BUY is unaffected — held
  qty is irrelevant to opening or adding to a position, only to closing one.
- One candidate failing (a guardrail refusal, an Alpaca error) is recorded as
  that candidate's own outcome and never stops the rest, and never raises
  into `run_checkpoint()` — a broken auto-apply run must not also break
  research/scoring/notification for the checkpoint.
- Every submitted trade is also journaled (`journal.record_entry()`, the same
  call the human `trading-agent journal` path makes), tagged with the
  recommendation's own component scores and, when present, its sell-pressure
  reasoning. Auto-apply used to bypass this entirely — `historical_hitrate()`
  and `propose_weight_adjustments()` learn from `data/journal/`, and with
  auto-apply doing most of the actual trading, skipping the journal meant the
  learning loop was blind to most of what the system does. A journal write
  failure is caught and logged, never turned into a submission error — the
  order already went through; only the audit note failed.

Turning `enabled` back to `false` is sufficient and complete: nothing else
needs to change, and no auto-approved history is rewritten or hidden by it.

**Confidence-scaled position sizing** (`recommendation_engine._suggested_size_pct()`,
gated by `position.confidence_scaled_sizing`, default `true`): a
just-over-the-floor call sizes at `min_position_pct_of_portfolio` (the
floor), scaling linearly up to `max_position_pct_of_portfolio` (the cap) at
confidence 100 — this feeds both auto-apply's qty and the
`suggested_size_pct_of_portfolio` a human sees when deciding their own qty.
The floor is `min_confidence_to_buy` for a BUY, `min_confidence_to_notify`
for a SELL (see "Minimum confidence to buy" below) — a BUY can never score
below its bar, so sizing it from that bar (not the lower notify threshold)
is what actually spans floor-to-cap instead of compressing every real BUY
into the top sliver of the range. `false` reverts to the original flat
behavior: always exactly the cap for any actionable recommendation,
confidence-blind.

## Portfolio guardrails (hard requirement)

`guardrails.py` holds nine checks. Each returns a refusal reason or `None`;
callers turn that into `RoutineHalted` (orchestrator) or `OrderRefused`
(order_manager). They are enforced at *both* boundaries — a rule that only
applies at execution time would let the routine spend a day proposing trades
it can never place.

1. **Max 5% of portfolio per position** — `position_size_reason()`, checked
   before any BUY. Counts the existing position in the same symbol, so
   repeated partial buys can't stack past the cap one approval at a time.
   Cap: `risk_limits.yaml -> position.max_position_pct_of_portfolio`.
2. **2% daily loss halts the routine** — `daily_loss_reason()`, measured as
   Alpaca `equity` vs. `last_equity` (prior close), so it resets each trading
   day with no state on disk. Past the cap, `run_checkpoint()` halts *before*
   spending research budget, and order submission refuses for the rest of the
   day. Cap: `risk_limits.yaml -> portfolio.max_daily_drawdown_pct`.
3. **No options, ever** — `is_option_symbol()` matches OCC contract symbols
   (`AAPL240119C00150000`). There is deliberately **no config key** for this:
   like `allow_live_trading`, lifting it takes a reviewed code change. Enforced
   in `order_manager` *and* again in `alpaca_client.submit_market_order()`, so
   it holds even for a caller that bypasses the gate. Option symbols are also
   filtered out of the candidate set in `orchestrator.run_checkpoint()`.
4. **No short positions, ever** — `short_sale_reason()`, checked before any
   SELL. A SELL is only ever a reduction of an existing long here; refuses
   outright if there's no existing position, or if the requested qty exceeds
   what's held (that remainder would open a short). The 5% cap in
   `position_size_reason()` applies to BUY alone, so without this check a
   SELL recommendation on a ticker you don't hold would open unbounded short
   exposure with no guardrail on it at all — there is deliberately no config
   key to allow shorting, same as the options ban.
5. **Max concurrent positions** — `position_count_reason()`, checked before
   any BUY that would open a symbol not already held. Closes a real gap: the
   5% cap is *per position*, so nothing previously stopped many individual
   BUYs — each legitimately under 5% on its own — from collectively consuming
   the whole account (confirmed live: a `pre_open` run where every
   recommendation shared one flat, wrong confidence score, see below, drove
   auto-apply toward exactly that). Adding to a symbol you already hold isn't
   a *new* position, so it isn't counted here — `position_size_reason()`
   already bounds that case. Cap: `risk_limits.yaml ->
   position.max_concurrent_positions`; unset/zero means no cap (fails open on
   a config that was never set, not on missing account data — see below).
6. **Max daily trade count, every source combined** — `daily_trade_count_reason()`,
   checked before any order (BUY or SELL, human or auto). Counts
   `data/trades/<today>/orders_submitted.json`, which every submitted order
   is already appended to, so there's nothing extra to keep in sync. Distinct
   from `operational.auto_apply.max_trades_per_day` (below): that one only
   paces auto_apply's own trades and never sees a human's; this is the real
   ceiling on the day's total order count regardless of who approved it. Cap:
   `risk_limits.yaml -> portfolio.max_daily_trades`; unset/zero means no cap.
7. **Stale recommendation re-check, right before submission** —
   `stale_recommendation_reason()`, checked before any order (BUY or SELL,
   human or auto) — the only guardrail that isn't itself an execution-time
   version of a proposal-time rule; there is no equivalent check at
   proposal time because the whole point is *what changed since then*. Two
   independent sub-checks: (a) current quote vs. the recommendation's own
   `reference_price` (the price `technical_score()` was actually computed
   from), refused past `execution.max_price_drift_pct`; (b) a fresh
   `technical_score()` recomputed from current bars, refused if the
   direction it implies no longer agrees with the recommendation's
   `action`. Either catches the same failure mode: a recommendation can sit
   for up to `operational.approval_expiry_hours` waiting on a human, and
   the market doesn't wait with it. `execution.reverify_technical: false`
   is a one-line revert to price-drift-only checking; an unset/missing
   `reference_price` (an older record, or an ad hoc one from
   `agents/tools.py`) skips the price check rather than failing — nothing
   to compare against, not a breach.
8. **Max sector concentration** — `sector_concentration_reason()`, checked
   before any BUY. Closes a gap the per-position and position-count caps
   both miss: several different positions can each individually pass those
   checks while the account is entirely one sector's risk. Sector comes from
   `data/market_data.py:get_sector()` (yfinance, free/keyless, same
   credential-policy reasoning as the news fallback), via
   `guardrails._sector_bucket()`, which folds every symbol yfinance can't
   classify (ETFs, warrants, some foreign/newer listings) into one synthetic
   `"Unclassified"` sector rather than exempting it — several individually-
   unclassified symbols used to each skip this check while collectively
   growing past the cap (confirmed live 2026-09-29: three unclassified
   warrants alone made up over 8% of the portfolio, invisible to this
   guardrail). Cap: `risk_limits.yaml -> portfolio.max_sector_concentration_pct`
   (30%, raised from 25% per user instruction 2026-09-29); unset/zero means
   no cap.
9. **Minimum confidence to buy** — `min_confidence_reason()`, checked before
   any BUY (never a SELL — see "Minimum confidence to buy" below). Backstop
   for the same bar `recommendation_engine.score_candidate()` already
   enforces at scoring time (a BUY below the bar is scored `HOLD`, never
   `BUY`), so this should be unreachable via the normal checkpoint ->
   approval -> execute path — it only catches a BUY assembled outside that
   path (an ad hoc one from `agents/tools.py`, or an older record predating
   this bar). Cap: `risk_limits.yaml -> position.min_confidence_to_buy`
   (85, per user instruction 2026-09-30); fails closed if the recommendation
   has no `confidence` field at all.

These **fail closed**: if Alpaca account state can't be read, the
account-dependent checks report a breach rather than assume the portfolio is
healthy. A guardrail that passes when it can't see anything isn't a
guardrail — every check here, sector concentration included, fails closed on
genuinely unreadable account/quote data; only the sector *label* itself is a
best-effort classification (unknown lands in `"Unclassified"`, not an
exemption).

## Research data quality (hard requirement)

Two related production fixes, both from the same incident (a `pre_open` run
where all 15 recommendations came back with an identical, wrong confidence of
72 and one auto-applied BUY blew past the per-position cap):

- **No recommendation is ever scored on missing research.**
  `research.perplexity_client.research_ticker()` tries Perplexity first; if
  that call fails outright or synthesizes nothing usable (no text, no
  sources), it falls back to `data/market_data.py`'s
  `fetch_news_headlines()` — free, keyless Yahoo Finance headlines, tagged
  `research_source: "yahoo_finance_fallback"` on the recommendation so the
  audit trail never confuses a fallback run with a real Perplexity one. If
  *both* sources come back empty, `research_ticker()` raises rather than
  returning a placeholder, and `orchestrator.run_checkpoint()`'s existing
  per-ticker try/except skips that ticker entirely (into `failed_tickers`) —
  analysis without real research data is refused, not guessed at. (Google was
  considered as a second fallback and deliberately left out: it would need a
  new API credential under the policy above, a reviewed decision, not
  something to wire in silently.)
- **A flat confidence score across many tickers is a data-quality failure,
  reported and blocked, never trusted.** The 72-for-everyone incident traced
  to `technical_score()` silently returning its neutral 0.5 fallback for
  every ticker — `get_recent_bars()`'s old 60-day default yielded fewer
  trading days than the 50-day SMA needs, so the "not enough history" branch
  fired every single time, for every ticker, without error. Two fixes:
  `get_recent_bars()`'s default is now 120 days (safe margin above 50 trading
  days), and — belt and suspenders — `score_candidate()` now takes
  `technical: float | None` and excludes it from the weighted sum (like
  `fundamental`/`historical_hitrate` already do) whenever there isn't enough
  bar history, rather than ever treating "not enough data" as a real neutral
  reading again. Because `technical` is the formula's only directional input,
  a missing one also now forces `action = "HOLD"` — no more guessing BUY from
  a placeholder. On top of that, `orchestrator.run_checkpoint()` checks after
  scoring whether ≥3 actionable (BUY/SELL) candidates share one identical
  confidence value; if so it's reported (`DATA QUALITY ALERT`, unconditional
  on console, headlined in the email/SMS digest via
  `notify_digest(data_quality_alert=...)`) and **auto-apply is skipped
  entirely for that checkpoint** — a flat score never reaches an order.

## Text-derived signals (sentiment/catalyst)

`sentiment` and `catalyst` used to be crude proxies — essentially "did
research return any sources at all" — not derived from what the research
text actually said, so two tickers with opposite news could still get near-
identical scores on these components. `scoring/text_signals.py` replaces both
with real keyword-lexicon scoring over the research's own headline/summary
text (`sentiment_score()`: bullish-vs-bearish term ratio;
`catalyst_score()`: hits against a catalyst-term lexicon, capped at 1.0).
Deliberately not a paid/credentialed NLP or sentiment API — that would be a
new vendor under the credential policy above, a reviewed decision. Either
function returns `None` when no keywords match, same "exclude, don't fake"
convention as every other component: `score_candidate()` drops it from the
weighted sum and renormalizes rather than scoring a silent 0.5.

## Momentum cap (recommendation_engine.py)

`technical_score()`'s momentum term used to be unbounded, so an
already-extended move fed the same signal that rewards a fresh breakout —
scoring reasons to chase a rally that's already run, not just to catch one
starting. `MOMENTUM_CAP` (0.10) clamps the 5-day momentum term before it
factors into the score, so a stock that's already moved far keeps
contributing at the cap rather than an ever-larger, cumulative momentum
number swamping the SMA-crossover trend term.

## Long-term trend signal: 30-day vs. 52-week average (recommendation_engine.py)

Per user instruction 2026-09-30 ("history of last 30 days vs 52 wk avg and
latest price ... should be included in the assessment"): `long_term_trend(bars)`
reports where the latest close sits versus its own 30-day and ~52-week
(`TREND_LOOKBACK_TRADING_DAYS`, 252 trading days) averages —
`{latest_price, avg_30d, avg_52wk, pct_vs_30d, pct_vs_52wk, bullish}`, where
`bullish` is `latest_price > avg_52wk`. `None` below `MIN_BARS_FOR_TREND`
(200, ~40 weeks) rather than computing a "52-week average" from a few
months of data — same "exclude, don't fake" convention as every other
component here, which also means most of the thinly-traded micro-cap/warrant
tickers this project has struggled with (recently listed, so no real
52-week track record) simply report no long-term trend at all, by design.

Folded directly into `technical_score()` (not a new weighted confidence
component — it shares that function's `bars` parameter, so it costs no
extra Alpaca call): averaged with `pct_vs_30d`, capped at `TREND_CAP` (0.15,
same saturating-cap pattern as `MOMENTUM_CAP`) so an extreme move doesn't
swamp the SMA-crossover/momentum terms it's blended with.
`orchestrator.run_checkpoint()` now fetches `TREND_FETCH_CALENDAR_DAYS`
(370) days of bars per ticker instead of just enough for the 50-day SMA, so
`technical_score()` and `long_term_trend()` share one fetch. The `bullish`
flag also feeds "Market-regime stop-loss dampening" below — recorded on
every recommendation as `long_term_trend` (`None` for a ticker without
enough history) for audit visibility.

## Minimum confidence to buy (recommendation_engine.py, guardrails.py)

Per user instruction 2026-09-30 ("consider any stock purchase only if
confidence level is at least 85%"): a bullish call only becomes `action:
"BUY"` at `risk_limits.yaml -> position.min_confidence_to_buy` (85, a
materially higher bar than `min_confidence_to_notify`'s 50). Below it,
`score_candidate()` reports `HOLD` — not "BUY at low confidence" — so a
human reviewing recommendations never sees a purchase idea this project
itself wouldn't act on; `guardrails.min_confidence_reason()` is the
execution-time backstop (guardrail #9 above). `_suggested_size_pct()`'s
confidence-scaled sizing floor for a BUY is this same bar (not
`min_confidence_to_notify`), so a real BUY's suggested size actually spans
floor-to-cap instead of being compressed into the top sliver of the range.

This bar applies to BUY only. A fresh bearish call, and a stop-loss/
take-profit-forced SELL, are both still gated by the existing (lower)
`min_confidence_to_notify` / `sell_pressure` logic exactly as before —
raising the bar on buying more is not raising the bar on cutting a loss;
those would be exactly backwards.

## Backtest parity (backtest/engine.py)

`backtest_moving_average()` (kept, from the original scaffold) only ever
validated a standalone SMA-crossover reimplementation — never the actual
deployed confidence formula or guardrails, so a passing backtest said nothing
about whether the live strategy was sound. `backtest_strategy()` instead
walks the real `technical_score()`/`score_candidate()` functions bar-by-bar
against historical data, simulating a single position (BUY opens it, SELL
closes it) and feeding simulated cost-basis P&L back in as
`position_pnl_pct`, the same input the live stop-loss/take-profit bias uses.
Returns CAGR/Sharpe/max-drawdown plus a buy-and-hold baseline. Its `note`
field discloses what it *can't* validate: `sentiment`/`catalyst`/
`fundamental`/`historical_hitrate` are always `None` here, since there's no
historical archive of research text to derive them from — only `technical`
(and, on a held position, the P&L-driven sell-pressure bias) is exercised.
`trading-agent backtest` prints both functions' results side by side.

## Broader candidate sourcing: losers, not just gainers

`data.alpaca_client.get_market_movers()` has always fetched both `gainers`
and `losers` from Alpaca's screener, but `orchestrator.run_checkpoint()`
only ever added the top-10 *gainers* to the research universe — the
candidate set was 100% biased toward names already up, compounding the
momentum-chasing risk `MOMENTUM_CAP` exists to bound rather than
counteracting it, and (combined with the static 5-ticker `watchlist.yaml`
and continuous position monitoring below) the same narrow slice of tickers
dominated day after day. Per user instruction 2026-09-30 ("ensure
assessment is done broadly"): the top-10 *losers* now seed candidates too,
through the exact same research/scoring/auto-apply path as everything
else — nothing here assumes a falling price means a buy or a sell,
`technical_score()` still decides that per ticker, same as always.

**Liquidity floor on new candidates**, per user instruction 2026-10-01: a
week's worth of live data showed APUS/ABLVW/GRMLW/DAICW/INLF/SOAR —
sub-$1 (several sub-$0.05) movers-turned-held-positions — were
simultaneously the account's biggest losers and the names most likely to
trip `stale_recommendation_reason()`'s 3% drift check (confirmed live: 24
of 49 logged auto-apply attempts that week were refused for exactly that,
because these specific tickers routinely move double digits in the
minutes between scoring and submission). `orchestrator._above_min_price()`
filters gainers/losers to `risk_limits.yaml -> position.candidate_min_price`
(1.0 by default) *before* taking the top 10 of each — filtering after the
slice would just shrink an already-penny-heavy list instead of reaching
past it for real candidates. A mover with no price at all is excluded
rather than assumed to pass (candidate sourcing, not an order guardrail,
so there's no fail-closed/fail-open question to get wrong either way — only
fewer candidates researched). Unset/zero means no floor.

Deliberately narrow in scope: this never touches `watchlist.yaml` (your
own curated list — a penny stock you add there yourself is a deliberate
choice, not something to filter) or anything already held (continuous
position monitoring below must keep scoring a held position regardless of
price, or a stop-loss SELL could never even be proposed for it). This fix
stops *new* junk candidates from entering the pipeline; it does nothing by
itself to help exit a penny stock already held — that's a separate,
not-yet-built problem (see the 2026-09-30/10-01 performance review:
severity-based auto-apply SELL prioritization and/or a wider drift
tolerance for exits specifically).

## Continuous position monitoring (plan §6)

Every checkpoint's research universe used to be exactly `watchlist.yaml` +
that run's top-10 gainer movers — a ticker bought today but never added to
the watchlist would drop out of scoring the moment it stopped being a
"mover," and no future checkpoint would ever propose a SELL for it again.
Fixed: `orchestrator._held_position_pnl_pct()` reads every current Alpaca
position at the start of `run_checkpoint()` and adds all of them to the
ticker set unconditionally, so anything you hold gets researched and scored
every checkpoint regardless of watchlist/movers status. A read failure
degrades to "no positions known" for that one run (same best-effort
tolerance as `get_market_movers()`) rather than blocking the checkpoint.

Each held ticker's live unrealized return (Alpaca's `unrealized_plpc`, as a
percent) is also passed into `score_candidate()` as `position_pnl_pct`,
which biases — never overrides — the action toward SELL via
`position_sell_pressure()`: both a stop-loss breach (cut losses) *and* a
take-profit breach (lock in gains) push toward SELL, for opposite reasons,
so this is a tent shape (0 well within range, 0.5 right at either
threshold, capping at 1.0 twice as far past it), not a simple bullish/
bearish scale. The bias multiplies into the technical signal used for
direction (`technical * (1 - sell_pressure)`) — a strongly bullish technical
read can still hold the line at BUY against a mild breach; a weak-to-moderate
one flips to SELL under the same pressure. `short_sale_reason()` already
bounds a SELL to what's actually held, so this can never manufacture a
short. Gated by `risk_limits.yaml -> position.mandatory_stop_loss` (a config
key that existed before but was never read anywhere) — `false` is the
one-line revert to pure-technical direction, unconditionally, same
convention as `auto_apply.enabled`. Thresholds:
`position.stop_loss_pct`/`take_profit_pct` (default 4.0/8.0) — previously
hardcoded into every recommendation's `stop_loss_pct`/`take_profit_pct`
fields with nothing ever reading them back; now those same config values
both label the recommendation *and* actually drive `sell_pressure`.
`position_pnl_pct` and `sell_pressure` are recorded on every recommendation
(`None`/`0.0` for a ticker that isn't currently held) for audit visibility
into *why* a SELL fired — a fresh bearish technical read and a stop-loss-
triggered one look identical in `action` alone otherwise.

Auto-apply treats a stop-loss/take-profit-biased SELL exactly like any other
recommendation — no special human-only gate — since every guardrail
(kill switch, short-sale ban, daily-loss halt, daily trade cap) already
applies unchanged regardless of what produced the `action`.

**Bug fix:** the stop-loss/take-profit bias used to be gated behind
`confidence >= min_confidence_to_notify`, so a held position with a severe
stop-loss breach but low blended confidence reported `HOLD` instead of
`SELL` — defeating the point of a stop-loss. `score_candidate()` now forces
`SELL` whenever `sell_pressure > 0` and the pressure-adjusted technical read
is bearish, independent of the notify threshold; the threshold still gates
whether a *fresh* (non-position) signal counts as actionable enough to
surface.

**Market-regime stop-loss dampening**, per user instruction 2026-09-30
("stock with promising increase should not be sold on losses ... due to
entire market having a downward trend"): a stop-loss breach — never a
take-profit one, locking in gains isn't something a market dip should block
— has its `sell_pressure` *dampened* (multiplied by
`position.market_regime_dampening_factor`, default 0.4 — not zeroed) when
BOTH the broader market is genuinely down (`orchestrator.run_checkpoint()`
reads `data.alpaca_client.get_market_return_pct("SPY")` once per checkpoint,
at or below `-position.market_regime_down_threshold_pct`, default 1.0) AND
the position's own long-term trend is still bullish (`long_term_trend()`
above, `bullish: true`). Deliberately narrow: it never fires without both a
down market and an *established* (200+ bar) uptrend, so a speculative name
with no real price history — most of the micro-cap/warrant tickers this
project has struggled with — gets no protection, same as it gets none from
`long_term_trend()` itself. Dampened rather than suppressed outright, since
a broad-market day doesn't fully rule out real stock-specific weakness
either. Recorded on every recommendation as `regime_dampened` (bool) and
`market_return_pct` for audit visibility. Gated by
`position.market_regime_stop_loss_dampening` (default `true`) — `false` is
the one-line revert, same convention as `mandatory_stop_loss`.

**Reacting faster on exits**, per user instruction 2026-09-30 ("make this
work more dynamically and react quicker"): `run_checkpoint()` now scores
held positions in their own, earlier pass — and calls `execute.auto_pilot.
auto_apply()` on that batch immediately — *before* the (usually larger)
watchlist/movers batch is even researched, instead of one combined pass
with a single `auto_apply()` call at the very end. Confirmed live
2026-09-29: every stop-loss-triggered SELL that day was refused, almost
certainly by `stale_recommendation_reason()`'s price-drift/technical-
reversal check — a held position researched early in a long, sequential
per-ticker research loop could sit for several minutes before `auto_apply()`
ever got to it, and by then a volatile micro-cap/warrant's price had often
already moved past `execution.max_price_drift_pct`. Scoring and executing
on held positions first minimizes that gap for exactly the case where
staleness matters most. `auto_apply()`'s daily cap (`operational.auto_apply.
max_trades_per_day`) is shared correctly across the two calls — it always
re-reads `data/trades/` fresh, so nothing double-counts.

## What's not built yet

- ~~The last hop of the improvement loop~~ (plan §6.3, build sequence phase 8) —
  built: `journal.aggregate_performance()` (`trading-agent journal aggregate`)
  rolls outcome-marked `data/journal/` entries up into
  `data/performance/strategy_metrics.json` — a full recompute every call, not
  an incremental merge. `by_ticker` is straight from each entry's own outcome;
  `by_signal_type` looks the entry's ticker+checkpoint back up in that day's
  `data/recommendations/` for `component_scores`, and credits a component when
  its own bullish/bearish lean agreed with which way the price actually moved,
  independent of the blended action. `historical_hitrate()` and
  `propose_weight_adjustments()` now read real numbers instead of nothing —
  but with an empty `data/journal/` so far (no checkpoint has run for real
  yet), both are still waiting on enough history to say anything.
- ~~Real P&L in the weekly report~~ — built: `build_weekly_report()` now adds
  a `## P&L` section. Realized P&L (`_realized_pnl()`) walks the week's
  `data/trades/` fills in submission order, average-cost basis per symbol —
  only orders with a confirmed fill (`filled_qty`/`filled_avg_price`) count;
  this project doesn't poll Alpaca for fill confirmation after submission, so
  an order recorded before it fills is excluded and reported separately as a
  pending-fill count rather than guessed at. Unrealized P&L (`_unrealized_pnl()`)
  is a live snapshot straight from Alpaca's own per-position figures — no
  reconstruction needed there.
- ~~A real notification channel~~ — built and confirmed live: `email` in
  `config/agent_config.yaml -> notifications.channel` sends real mail via
  Resend's HTTPS API (not SMTP, which times out in this project's cloud
  sandbox), one consolidated message per checkpoint (`notify_digest()`) rather
  than one per ticker. `sms` (a carrier email-to-SMS gateway, same mechanism)
  is implemented but not in the default channel list — Resend's no-setup
  sandbox sender can only send to the account's own verified email, so it
  403s on an SMS gateway address until `RESEND_FROM_ADDRESS` is on a verified
  domain (see the credential policy section above). `trading-agent
  notify-test` fires a one-off message through whatever's configured. Slack
  and push are still just the docstring's aspiration, not built.
- **A secondary fundamentals/screening vendor** — Alpaca's own coverage is
  limited (plan §5); `fundamental` score currently defaults to neutral (0.5)
  in `orchestrator.py`.

## Running

```
pip install -e ".[dev]"
# Set PERPLEXITY_API_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY in your shell — never in a file.

trading-agent checkpoint pre_open
trading-agent approvals list pre_open
trading-agent approvals approve TSLA pre_open --qty 10
trading-agent execute TSLA pre_open 10       # still refuses unless trading_enabled: true
trading-agent report daily
pytest
```

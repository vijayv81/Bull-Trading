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
  reporting/               report_builder.py — daily/weekly markdown reports + realized/unrealized P&L
  agents/                  interactive Claude Agent SDK research (see above)
  backtest/                unchanged from the original scaffold
routines/                 trading_checkpoints.md — spec for the 4 scheduled routines (/schedule)
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
  whole shares at the current quote — never rounds up.
- One candidate failing (a guardrail refusal, an Alpaca error) is recorded as
  that candidate's own outcome and never stops the rest, and never raises
  into `run_checkpoint()` — a broken auto-apply run must not also break
  research/scoring/notification for the checkpoint.

Turning `enabled` back to `false` is sufficient and complete: nothing else
needs to change, and no auto-approved history is rewritten or hidden by it.

**Confidence-scaled position sizing** (`recommendation_engine._suggested_size_pct()`,
gated by `position.confidence_scaled_sizing`, default `true`): a
just-over-the-notify-threshold call sizes at `min_position_pct_of_portfolio`
(the floor), scaling linearly up to `max_position_pct_of_portfolio` (the cap)
at confidence 100 — this feeds both auto-apply's qty and the
`suggested_size_pct_of_portfolio` a human sees when deciding their own qty.
`false` reverts to the original flat behavior: always exactly the cap for any
actionable recommendation, confidence-blind.

## Portfolio guardrails (hard requirement)

`guardrails.py` holds six checks. Each returns a refusal reason or `None`;
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

These **fail closed**: if Alpaca account state can't be read, the
account-dependent checks report a breach rather than assume the portfolio is
healthy. A guardrail that passes when it can't see anything isn't a guardrail.

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

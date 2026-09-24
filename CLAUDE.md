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
Resend's no-setup sandbox sender, `onboarding@resend.dev`, which works without
verifying a domain). `NOTIFY_EMAIL_ADDRESS` (the email destination) and
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

`guardrails.py` holds four checks. Each returns a refusal reason or `None`;
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

These **fail closed**: if Alpaca account state can't be read, the
account-dependent checks report a breach rather than assume the portfolio is
healthy. A guardrail that passes when it can't see anything isn't a guardrail.

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
- ~~A real notification channel~~ — built: `notify/senders.py` sends email
  and SMS (via a carrier email-to-SMS gateway) over Resend's HTTPS API — not
  SMTP, which times out in this project's cloud sandbox — one consolidated
  message per checkpoint (`notify_digest()`, wired into
  `orchestrator.run_checkpoint()`) rather than one per ticker. Add `email`
  and/or `sms` to `config/agent_config.yaml -> notifications.channel` and set
  the env vars in CLAUDE.md's credential policy section; `trading-agent
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

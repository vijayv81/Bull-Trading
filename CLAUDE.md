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
  orchestrator.py          run_checkpoint(): research -> data -> score -> notify
  research/                perplexity_client.py
  data/                    alpaca_client.py (primary), market_data.py (yfinance, backtest-only)
  journal.py              decision reasoning + outcome measurement (plan §6.3, partial)
  scoring/                 recommendation_engine.py — confidence formula (plan §6)
  notify/                  approval_gateway.py — hard requirement gate (plan §8)
  execute/                 order_manager.py — approval + kill-switch gated Alpaca submission
  reporting/               report_builder.py — daily/weekly markdown reports
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

## Portfolio guardrails (hard requirement)

`guardrails.py` holds three checks. Each returns a refusal reason or `None`;
callers turn that into `RoutineHalted` (orchestrator) or `OrderRefused`
(order_manager). They are enforced at *both* boundaries — a rule that only
applies at execution time would let the routine spend a day proposing trades
it can never place.

1. **Max 5% of portfolio per position** — `position_size_reason()`, checked
   before any BUY. Counts the existing position in the same symbol, so
   repeated partial buys can't stack past the cap one approval at a time.
   SELLs reduce exposure and are never blocked. Cap:
   `risk_limits.yaml -> position.max_position_pct_of_portfolio`.
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

These **fail closed**: if Alpaca account state can't be read, the two
account-dependent checks report a breach rather than assume the portfolio is
healthy. A guardrail that passes when it can't see anything isn't a guardrail.

## What's not built yet

- **The last hop of the improvement loop** (plan §6.3, build sequence phase 8).
  `journal.py` now does the measuring — it records the human's reasoning at
  decision time and marks what the price did afterwards, into `data/journal/`.
  What's still missing is the aggregation: nothing rolls those outcomes up into
  `data/performance/strategy_metrics.json`, so `historical_hitrate()` still
  returns its neutral 0.5 default and `propose_weight_adjustments()` still has
  nothing to propose from. Wiring that up changes how every future confidence
  score is computed, so it wants its own reviewed commit once there's enough
  journal history to aggregate.
- **Real P&L in the weekly report** — `reporting/report_builder.py` currently
  reports activity counts (recs/approvals/trades), not realized/unrealized
  P&L, which needs position-marking logic.
- **A real notification channel** — defaults to console/file
  (`config/agent_config.yaml -> notifications.channel`); `notify/
  approval_gateway.py:notify()` is the single integration point once you
  pick push/email/Slack/SMS.
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

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

## Layout

```
config/                  watchlist.yaml, risk_limits.yaml (kill switch lives here), agent_config.yaml
src/trading_agent/
  config.py               YAML config + require_env() — the ONLY way credentials are read
  utils.py                shared date-partitioned file-layer helpers
  orchestrator.py          run_checkpoint(): research -> data -> score -> notify
  research/                perplexity_client.py
  data/                    alpaca_client.py (primary), market_data.py (yfinance, backtest-only)
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

## What's not built yet

- **Outcome tracking / continuous improvement loop** (plan §6.3, build
  sequence phase 8): nothing yet compares a recommendation's confidence
  against what the price actually did. `data/performance/strategy_metrics.json`
  is seeded with neutral 0.5 defaults; `scoring/recommendation_engine.py:
  historical_hitrate()` reads from it, and `propose_weight_adjustments()` is
  ready to consume it once it's populated — but the "did this call work out"
  measurement itself isn't implemented.
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

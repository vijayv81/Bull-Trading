# trading-agent

Personal trading research/recommendation toolkit built on the Claude Agent SDK
(Python, package `claude-agent-sdk`). Not a trading bot — it never places or
simulates order execution; it produces research notes and buy/sell/hold signals
for the user to read.

## Layout

- `src/trading_agent/data/` — market data ingestion (`yfinance`) and local
  parquet/JSON storage under `data/raw` and `data/processed`.
- `src/trading_agent/strategy/` — turns price history into a signal +
  recommendation record. `moving_average_signal` is a placeholder strategy;
  swap in real logic here.
- `src/trading_agent/backtest/` — vectorized backtest engine + metrics
  (CAGR, Sharpe, max drawdown) for scoring a strategy against history.
- `src/trading_agent/agents/` — the Claude Agent SDK wiring: system prompt
  (`prompts/research_system_prompt.md`), custom tools exposed via an in-process
  MCP server (`tools.py`), and the orchestrator that builds `ClaudeAgentOptions`
  and runs `query()` (`orchestrator.py`).
- `src/trading_agent/cli.py` — `trading-agent ingest|recommend|backtest|research`.
- `routines/` — specs for scheduled Claude Code routines (see `/schedule`);
  nothing in this folder runs on its own.

## Conventions

- All recommendations must include a rationale and the research-only caveat —
  enforced in the system prompt and in `strategy/recommendation.py`.
- Cached market data and generated recommendations live under `data/` and are
  gitignored; only the directory structure is tracked.
- Config comes from `.env` (see `.env.example`) via `trading_agent/config.py`.

## Running

```
pip install -e ".[dev]"
cp .env.example .env   # fill in ANTHROPIC_API_KEY if not using `ant auth login`
trading-agent ingest
trading-agent recommend
trading-agent backtest
trading-agent research "Analyze NVDA and recommend a signal"
pytest
```

"""Custom tools exposed to the interactive Claude research agent via an
in-process MCP server. Separate from the automated checkpoint pipeline
(orchestrator.py) — this is for ad hoc `trading-agent chat "..."` sessions,
but writes into the same data/recommendations/ file layer so both paths are
auditable from one place (plan §1.2 design principle #1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from trading_agent.data.market_data import cache_price_history
from trading_agent.notify.approval_gateway import save_recommendation as _save_recommendation
from trading_agent.scoring.recommendation_engine import technical_score


@tool(
    "get_price_history",
    "Fetch and cache recent OHLCV price history for a ticker, returning summary stats.",
    {"ticker": str, "period": str},
)
async def get_price_history(args: dict[str, Any]) -> dict[str, Any]:
    ticker = args["ticker"].upper()
    period = args.get("period") or "6mo"
    df = cache_price_history(ticker, period=period)
    close = df["Close"]
    summary = {
        "ticker": ticker,
        "last_close": float(close.iloc[-1]),
        "pct_change_1d": float(close.pct_change().iloc[-1] * 100),
        "pct_change_20d": float(close.pct_change(20).iloc[-1] * 100) if len(close) > 20 else None,
        "technical_score": technical_score(df.rename(columns={"Close": "close"})),
    }
    return {"content": [{"type": "text", "text": str(summary)}]}


@tool(
    "save_recommendation",
    "Persist a research recommendation (ticker, signal, rationale) for later review.",
    {"ticker": str, "signal": str, "rationale": str},
)
async def save_recommendation(args: dict[str, Any]) -> dict[str, Any]:
    rec = {
        "ticker": args["ticker"].upper(),
        "checkpoint": "adhoc",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": args["signal"].upper(),
        "confidence": 100.0,
        "component_scores": {},
        "suggested_size_pct_of_portfolio": 0.0,
        "stop_loss_pct": None,
        "take_profit_pct": None,
        "rationale": args["rationale"],
        "note": "Manually recorded via interactive Claude research session, not the automated pipeline.",
    }
    _save_recommendation(rec)
    return {"content": [{"type": "text", "text": f"Saved recommendation for {rec['ticker']}"}]}


trading_tools_server = create_sdk_mcp_server(
    name="trading_tools",
    version="1.0.0",
    tools=[get_price_history, save_recommendation],
)

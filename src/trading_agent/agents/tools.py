"""Custom tools exposed to the Claude agent via an in-process MCP server."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from trading_agent.data.market_data import cache_price_history
from trading_agent.data.storage import save_json
from trading_agent.strategy.recommendation import build_recommendation


@tool(
    "get_price_history",
    "Fetch and cache recent OHLCV price history for a ticker, returning summary stats.",
    {"ticker": str, "period": str},
)
async def get_price_history(args: dict[str, Any]) -> dict[str, Any]:
    ticker = args["ticker"].upper()
    period = args.get("period") or "6mo"
    df = cache_price_history(ticker, period=period)
    summary = build_recommendation(ticker, df)
    return {"content": [{"type": "text", "text": str(summary)}]}


@tool(
    "save_recommendation",
    "Persist a research recommendation (ticker, signal, rationale) for later review.",
    {"ticker": str, "signal": str, "rationale": str},
)
async def save_recommendation(args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "ticker": args["ticker"].upper(),
        "signal": args["signal"],
        "rationale": args["rationale"],
    }
    path = save_json("recommendation", payload)
    return {"content": [{"type": "text", "text": f"Saved recommendation to {path}"}]}


trading_tools_server = create_sdk_mcp_server(
    name="trading_tools",
    version="1.0.0",
    tools=[get_price_history, save_recommendation],
)

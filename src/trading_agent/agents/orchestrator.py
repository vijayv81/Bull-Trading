"""Builds the Claude Agent SDK options and runs a one-off research query."""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from trading_agent.agents.tools import trading_tools_server
from trading_agent.config import MODEL

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


def research_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=_read_prompt("research_system_prompt.md"),
        model=MODEL,
        mcp_servers={"trading": trading_tools_server},
        allowed_tools=["mcp__trading__get_price_history", "mcp__trading__save_recommendation"],
        permission_mode="acceptEdits",
        max_turns=8,
    )


async def run_research(prompt: str) -> list:
    """Run a single research prompt through the agent and return the collected messages."""
    messages = []
    async for message in query(prompt=prompt, options=research_options()):
        messages.append(message)
    return messages

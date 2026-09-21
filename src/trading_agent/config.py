"""Project-wide configuration, loaded from .env / environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / os.environ.get("DATA_DIR", "data")
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"

MODEL = os.environ.get("TRADING_AGENT_MODEL", "claude-opus-5")

WATCHLIST = [
    ticker.strip()
    for ticker in os.environ.get("WATCHLIST", "AAPL,MSFT,NVDA,SPY").split(",")
    if ticker.strip()
]

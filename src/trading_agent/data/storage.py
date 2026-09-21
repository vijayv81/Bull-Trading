"""Small helpers for reading/writing processed artifacts (signals, recommendations)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_agent.config import PROCESSED_DATA_DIR


def save_json(name: str, payload: dict[str, Any]) -> Path:
    """Write a timestamped JSON artifact under data/processed/."""
    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = PROCESSED_DATA_DIR / f"{name}_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def latest_json(name_prefix: str) -> dict[str, Any] | None:
    """Return the most recently saved JSON artifact whose filename starts with name_prefix."""
    matches = sorted(PROCESSED_DATA_DIR.glob(f"{name_prefix}_*.json"))
    if not matches:
        return None
    return json.loads(matches[-1].read_text())

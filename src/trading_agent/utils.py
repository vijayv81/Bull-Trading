"""Shared helpers for the date-partitioned file layout (plan §3).

Every checkpoint writes into data/<category>/YYYY-MM-DD/ — these helpers keep
that convention in one place instead of re-implemented per module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def day_dir(base: Path, day: str | None = None) -> Path:
    path = base / (day or today())
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


def append_json(path: Path, item: dict[str, Any]) -> None:
    existing = load_json_list(path)
    existing.append(item)
    path.write_text(json.dumps(existing, indent=2, default=str))

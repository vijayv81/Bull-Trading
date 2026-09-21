"""Project-wide configuration: paths and YAML config loading.

Credential policy (plan §7.1, hard requirement): no secret is ever read from
or written to a file anywhere in this project. Config files may name the
environment variable a client should read (see config/agent_config.yaml
-> credentials); only require_env() supplies the actual value, straight from
the process environment. There is deliberately no .env file and nothing here
reads one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = PROJECT_ROOT / "config"

DATA_DIR = PROJECT_ROOT / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"
RECOMMENDATIONS_DIR = DATA_DIR / "recommendations"
APPROVALS_DIR = DATA_DIR / "approvals"
TRADES_DIR = DATA_DIR / "trades"
PERFORMANCE_DIR = DATA_DIR / "performance"

REPORTS_DIR = PROJECT_ROOT / "reports"
LOGS_DIR = PROJECT_ROOT / "logs"


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    return yaml.safe_load(path.read_text()) or {}


def load_watchlist() -> list[str]:
    return list(_load_yaml("watchlist.yaml").get("tickers", []))


def load_risk_limits() -> dict[str, Any]:
    return _load_yaml("risk_limits.yaml")


def load_agent_config() -> dict[str, Any]:
    return _load_yaml("agent_config.yaml")


def require_env(var_name: str) -> str:
    """Read a required credential from the environment; fail fast if missing.

    Never falls back to a file, a default, or a prompt — secrets live only in
    the process environment for the life of the run (plan §7.1).
    """
    value = os.environ.get(var_name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {var_name!r} is not set. "
            "Set it in your shell/session environment — never in a file."
        )
    return value

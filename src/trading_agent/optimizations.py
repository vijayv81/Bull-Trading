"""Clickable optimization options: turning the weekly learning review's
findings into concrete, one-click config changes.

The review finds patterns (a signal that keeps being wrong, a guardrail that
blocks most attempted orders); this module turns each into an *option*: an
exact config edit with its evidence. The weekly email links each option to
the Optimization Ticket page, where a click records Apply or Dismiss; the
next checkpoint routine runs `trading-agent optimizations apply <id>` for
each clicked Apply, then commits the change to git.

A click is the human decision; this module only carries it out, and only
within hard limits that live in code, not config:

- Only settings in CLICKABLE_SETTINGS can change, each within fixed bounds.
  The kill switch, live trading, auto-apply on/off, the options and short
  bans, and the position/loss caps are deliberately absent — changing those
  stays a hand edit.
- apply_option() reads the option from data/optimizations/, never from the
  click payload, so a hand-crafted link can only apply an option this module
  itself proposed.
- It refuses if the config no longer holds the value the option was
  proposed against: the evidence was about that value, not a newer one.
- Scoring weights must still sum to 1.0 afterward.
- Every apply and dismiss is appended to data/optimizations/ (tracked in
  git), and the config edit lands as its own commit, so any change is one
  revert away.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trading_agent.config import CONFIG_DIR, DATA_DIR, load_agent_config, load_risk_limits
from trading_agent.utils import append_json, day_dir, load_json_list

OPTIMIZATIONS_DIR = DATA_DIR / "optimizations"

# path -> (config file, min, max). Anything not listed here can't be changed
# by a click, however the option was produced.
CLICKABLE_SETTINGS: dict[str, tuple[str, float, float]] = {
    "scoring_weights.sentiment": ("agent_config.yaml", 0.0, 0.6),
    "scoring_weights.technical": ("agent_config.yaml", 0.0, 0.6),
    "scoring_weights.fundamental": ("agent_config.yaml", 0.0, 0.6),
    "scoring_weights.catalyst": ("agent_config.yaml", 0.0, 0.6),
    "scoring_weights.historical_hitrate": ("agent_config.yaml", 0.0, 0.6),
    "execution.max_price_drift_pct": ("risk_limits.yaml", 1.0, 5.0),
    "operational.approval_expiry_hours": ("risk_limits.yaml", 1.0, 6.0),
}

WEIGHT_STEP = 0.05
DRIFT_STEP_PCT = 1.0
EXPIRY_STEP_HOURS = 1
# Minimum evidence before an option is offered at all.
MIN_LOGGED_REFUSALS = 10
STALE_REFUSAL_SHARE = 0.5
MIN_NEVER_DECIDED = 10
# A scoring-weight proposal made within this many days of the last applied
# weight change carries a visible caution (never a block): the daily proposal
# re-reads rolling hit rates that barely move day to day, so a shift right
# after one is likely noise-chasing. The user decides; a hard block meant the
# email printed "consider reducing X's weight" with no way to act on it.
WEIGHT_CAUTION_DAYS = 7
# How long an unmerged daily-proposal PR keeps appearing in the daily email.
PR_REMINDER_DAYS = 7


class OptimizationRefused(Exception):
    """An option that can't be applied as-is — expected control flow, same
    spirit as execute.order_manager.OrderRefused."""


def _get_path(config: dict[str, Any], dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _current_values() -> dict[str, Any]:
    agent, risk = load_agent_config(), load_risk_limits()
    return {
        path: _get_path(agent if file == "agent_config.yaml" else risk, path)
        for path, (file, _, _) in CLICKABLE_SETTINGS.items()
    }


def _change(path: str, current: Any, proposed: Any) -> dict[str, Any]:
    return {"file": CLICKABLE_SETTINGS[path][0], "path": path, "from": current, "to": proposed}


def _last_weight_change(days: int, now: datetime | None = None) -> dict[str, Any] | None:
    """The most recent applied option that changed a scoring weight within the
    last `days`, or None."""
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - days * 86400
    latest: dict[str, Any] | None = None
    for day in _option_days():
        for rec in load_json_list(OPTIMIZATIONS_DIR / day / "applied.json"):
            if not any(str(c.get("path", "")).startswith("scoring_weights.") for c in rec.get("changes", [])):
                continue
            try:
                when = datetime.fromisoformat(rec["applied_at"])
            except (KeyError, ValueError):
                continue
            if when.timestamp() >= cutoff and (latest is None or rec["applied_at"] > latest["applied_at"]):
                latest = rec
    return latest


def _recent_change_caution(now: datetime | None = None) -> str | None:
    recent = _last_weight_change(WEIGHT_CAUTION_DAYS, now)
    if recent is None:
        return None
    now = now or datetime.now(timezone.utc)
    age = max((now - datetime.fromisoformat(recent["applied_at"])).days, 0)
    return (
        f"Scoring weights were last changed on {recent['applied_at'][:10]} ({age} day(s) ago): "
        f"{recent['title']}. The hit rates behind this proposal have had little time to change, "
        "so consider waiting."
    )


def build_options(review: dict[str, Any]) -> list[dict[str, Any]]:
    """Concrete options from a build_weekly_learning_review() result. Each is
    only offered when the review's own data clears a minimum-evidence bar,
    and each states plainly whether it loosens a guardrail."""
    from trading_agent.scoring.recommendation_engine import signals_with_enough_outcomes

    current = _current_values()
    window_end = review["window_end"]
    options: list[dict[str, Any]] = []

    signals = signals_with_enough_outcomes(review.get("signal_hit_rates") or {})
    if len(signals) >= 2:
        ranked = sorted(signals.items(), key=lambda kv: kv[1].get("hit_rate", 0.5))
        (worst, worst_stats), (best, best_stats) = ranked[0], ranked[-1]
        worst_path, best_path = f"scoring_weights.{worst}", f"scoring_weights.{best}"
        worst_w, best_w = current.get(worst_path), current.get(best_path)
        if (
            worst_stats.get("hit_rate", 0.5) < best_stats.get("hit_rate", 0.5) - 0.15
            and worst_path in CLICKABLE_SETTINGS
            and best_path in CLICKABLE_SETTINGS
            and worst_w is not None
            and best_w is not None
            and worst_w - WEIGHT_STEP >= CLICKABLE_SETTINGS[worst_path][1]
            and best_w + WEIGHT_STEP <= CLICKABLE_SETTINGS[best_path][2]
        ):
            options.append(
                {
                    "id": f"{window_end}-weights-{worst}-to-{best}",
                    "title": f"Shift {WEIGHT_STEP:g} of scoring weight from {worst} to {best}",
                    "why": (
                        f"{worst} has agreed with actual price direction {worst_stats['hit_rate']:.0%} of the time "
                        f"over {worst_stats['n']} measured calls, vs. {best}'s {best_stats['hit_rate']:.0%} "
                        f"over {best_stats['n']}."
                    ),
                    "effect": f"Confidence scores lean slightly more on {best} and less on {worst}.",
                    "loosens_guardrail": False,
                    "changes": [
                        _change(worst_path, worst_w, round(worst_w - WEIGHT_STEP, 4)),
                        _change(best_path, best_w, round(best_w + WEIGHT_STEP, 4)),
                    ],
                }
            )
            caution = _recent_change_caution()
            if caution:
                options[-1]["risk"] = caution

    breakdown = review.get("refusal_breakdown") or {}
    refused = sum(breakdown.values())
    stale = breakdown.get("stale: price drift", 0) + breakdown.get("stale: technical reversal", 0)
    drift_path = "execution.max_price_drift_pct"
    drift = current.get(drift_path)
    if refused >= MIN_LOGGED_REFUSALS and stale / refused >= STALE_REFUSAL_SHARE and drift is not None:
        proposed = min(round(drift + DRIFT_STEP_PCT, 2), CLICKABLE_SETTINGS[drift_path][2])
        if proposed > drift:
            options.append(
                {
                    "id": f"{window_end}-max-price-drift-{proposed:g}",
                    "title": f"Raise the stale-price limit from {drift:g}% to {proposed:g}%",
                    "why": (
                        f"{stale} of {refused} logged auto-apply refusals ({stale / refused:.0%}; "
                        f"{review.get('attempt_log_span', 'logged days only')}) were blocked because the "
                        f"price or signal moved between scoring and submission."
                    ),
                    "effect": (
                        f"Orders whose price moved up to {proposed:g}% since scoring can go through "
                        "instead of being refused."
                    ),
                    "loosens_guardrail": True,
                    "risk": (
                        "Loosens a guardrail: some of those blocked orders would have lost money (see "
                        "'Opportunities lost or avoided'). The technical re-check still runs."
                    ),
                    "changes": [_change(drift_path, drift, proposed)],
                }
            )

    never = len(review.get("never_decided") or [])
    expiry_path = "operational.approval_expiry_hours"
    expiry = current.get(expiry_path)
    if never >= MIN_NEVER_DECIDED and expiry is not None:
        proposed = min(expiry + EXPIRY_STEP_HOURS, int(CLICKABLE_SETTINGS[expiry_path][2]))
        if proposed > expiry:
            options.append(
                {
                    "id": f"{window_end}-approval-expiry-{proposed:g}h",
                    "title": f"Keep recommendations approvable for {proposed:g}h instead of {expiry:g}h",
                    "why": (
                        f"{never} actionable recommendations over the last {review.get('lookback_days')} days "
                        "expired with no decision recorded."
                    ),
                    "effect": "More time to approve or reject from the email before a recommendation lapses.",
                    "loosens_guardrail": True,
                    "risk": (
                        "An older approval can still execute; the stale-price re-check at submission is "
                        "what limits how far the market can have moved."
                    ),
                    "changes": [_change(expiry_path, expiry, proposed)],
                }
            )

    # Ids travel as the email link's #anchor and the ticket page's database
    # doc id; both only accept letters, digits and . _ ~ -
    for option in options:
        option["id"] = re.sub(r"[^A-Za-z0-9._~-]", "-", option["id"])
    return options


def save_options(options: list[dict[str, Any]], day: str) -> Path:
    path = day_dir(OPTIMIZATIONS_DIR, day) / "options.json"
    path.write_text(json.dumps(options, indent=2, default=str))
    return path


def add_options(options: list[dict[str, Any]], day: str) -> Path:
    """save_options() that keeps what's already saved for `day` (the weekly
    review and the daily proposal can both write the same day's file)."""
    path = day_dir(OPTIMIZATIONS_DIR, day) / "options.json"
    existing = load_json_list(path)
    have = {o["id"] for o in existing}
    path.write_text(json.dumps(existing + [o for o in options if o["id"] not in have], indent=2, default=str))
    return path


def _changes_key(option: dict[str, Any]) -> str:
    return json.dumps(option["changes"], sort_keys=True)


def record_pr(option_id: str, pr_url: str) -> dict[str, Any]:
    """Remember the PR opened for an option, so every later daily email can
    keep linking it until it's merged or ages out. Without this a proposal
    that was still unmerged the next day found itself "already pending" and
    the email had no link at all."""
    found = find_option(option_id)
    if found is None:
        raise OptimizationRefused(f"No saved option with id {option_id!r} in data/optimizations/.")
    day, _ = found
    path = OPTIMIZATIONS_DIR / day / "prs.json"
    records = [r for r in load_json_list(path) if r.get("option_id") != option_id]
    record = {"option_id": option_id, "pr_url": pr_url, "recorded_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(records + [record], indent=2))
    return record


def pr_url_for(option_id: str) -> str | None:
    for day in _option_days():
        for rec in load_json_list(OPTIMIZATIONS_DIR / day / "prs.json"):
            if rec.get("option_id") == option_id:
                return rec.get("pr_url")
    return None


def propose_daily(day: str, signal_hit_rates: dict[str, Any]) -> list[dict[str, Any]]:
    """The daily summary's learning proposal as a saved, applicable option.

    Only the scoring-weight shift: the refusal and expiry options are built
    from the weekly review's 30-day evidence and stay weekly. Same
    minimum-evidence bar as the weekly review (build_options()).

    Returns the options that still NEED a PR opened for them: a new one, or an
    identical pending option that never got a PR. An identical option that
    already has a PR recorded is left alone — its PR keeps appearing in the
    daily email (options_awaiting_review())."""
    pending = {_changes_key(o): o for o in pending_options()}
    needs_pr: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    for option in build_options({"window_end": day, "signal_hit_rates": signal_hit_rates}):
        match = pending.get(_changes_key(option))
        if match is None:
            fresh.append(option)
            needs_pr.append(option)
        elif pr_url_for(match["id"]) is None:
            needs_pr.append(match)
    if fresh:
        add_options(fresh, day)
    return needs_pr


def options_awaiting_review(day: str) -> list[dict[str, Any]]:
    """Open options with a recorded PR, proposed within PR_REMINDER_DAYS of
    `day`, each carrying its `pr_url` — what the daily email links. A merged
    PR drops out on its own (the config moved, so the option is no longer
    open); an abandoned one stops appearing after PR_REMINDER_DAYS."""
    cutoff = datetime.fromisoformat(day).timestamp() - PR_REMINDER_DAYS * 86400
    out = []
    for option in open_options():
        url = pr_url_for(option["id"])
        if url and datetime.fromisoformat(option["proposed_on"]).timestamp() >= cutoff:
            out.append({**option, "pr_url": url})
    return out


def options_proposed_on(day: str) -> list[dict[str, Any]]:
    """Options saved for `day` that can still apply (what the daily email's
    review section describes)."""
    return [o for o in open_options() if o["proposed_on"] == day]


def _option_days() -> list[str]:
    if not OPTIMIZATIONS_DIR.exists():
        return []
    return sorted((p.name for p in OPTIMIZATIONS_DIR.iterdir() if p.is_dir()), reverse=True)


def find_option(option_id: str) -> tuple[str, dict[str, Any]] | None:
    """(day, option) for an id from any saved options.json, newest day first."""
    for day in _option_days():
        for option in load_json_list(OPTIMIZATIONS_DIR / day / "options.json"):
            if option.get("id") == option_id:
                return day, option
    return None


def _decided_ids(filename: str) -> set[str]:
    return {
        rec["option_id"]
        for day in _option_days()
        for rec in load_json_list(OPTIMIZATIONS_DIR / day / filename)
    }


def pending_options() -> list[dict[str, Any]]:
    """Saved options not yet applied or dismissed, newest first."""
    done = _decided_ids("applied.json") | _decided_ids("dismissed.json")
    return [
        {**option, "proposed_on": day}
        for day in _option_days()
        for option in load_json_list(OPTIMIZATIONS_DIR / day / "options.json")
        if option["id"] not in done
    ]


def open_options() -> list[dict[str, Any]]:
    """pending_options() narrowed to the ones that can still apply: every
    change's `from` still matches today's config. An older option whose
    setting has since moved would only be refused by apply_option(), so
    offering it again is just noise."""
    current = _current_values()
    return [
        option
        for option in pending_options()
        if all(current.get(c["path"]) == c["from"] for c in option["changes"])
    ]


_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_]+):(?P<rest>.*)$")


def set_yaml_scalar(text: str, dotted: str, value: Any) -> str:
    """Replace one scalar in YAML text in place, keeping every comment and
    the rest of the file byte-for-byte — a yaml.safe_dump round trip would
    drop the comments that document why each setting is what it is."""
    parts = dotted.split(".")
    lines = text.splitlines(keepends=True)
    stack: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        match = _KEY_LINE.match(line.rstrip("\n"))
        if not match or line.lstrip().startswith("#"):
            continue
        indent = len(match["indent"])
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, match["key"]))
        if [key for _, key in stack] != parts:
            continue
        comment = ""
        rest = match["rest"]
        if "#" in rest:
            rest, comment = rest.split("#", 1)
            comment = " #" + comment.rstrip("\n")
        if not rest.strip():
            raise OptimizationRefused(f"{dotted} is a mapping, not a scalar setting.")
        newline = "\n" if line.endswith("\n") else ""
        lines[i] = f"{match['indent']}{match['key']}: {value}{comment}{newline}"
        return "".join(lines)
    raise OptimizationRefused(f"{dotted} not found in config.")


def apply_option(
    option_id: str, decided_by: str = "email-link", acknowledged_loosening: bool = False
) -> dict[str, Any]:
    """Apply a saved option's config changes. Refuses (OptimizationRefused)
    rather than partially applying when anything about it no longer holds.

    An option that loosens a guardrail also needs `acknowledged_loosening`:
    the Optimization Ticket page only records an apply for one after the
    viewer ticks an explicit acknowledgment, and the caller passes that
    through. A plain apply of a loosening option is refused."""
    found = find_option(option_id)
    if found is None:
        raise OptimizationRefused(f"No saved option with id {option_id!r} in data/optimizations/.")
    day, option = found
    if option_id in _decided_ids("applied.json"):
        raise OptimizationRefused(f"{option_id} was already applied.")
    if option.get("loosens_guardrail") and not acknowledged_loosening:
        raise OptimizationRefused(
            f"{option_id} loosens a guardrail; applying it needs an explicit acknowledgment "
            "(the ticket's checkbox, or --acknowledge-loosening on the command line)."
        )

    current = _current_values()
    new_text: dict[str, str] = {}
    for change in option["changes"]:
        path = change["path"]
        if path not in CLICKABLE_SETTINGS:
            raise OptimizationRefused(f"{path} is not a clickable setting.")
        file, low, high = CLICKABLE_SETTINGS[path]
        if change["file"] != file:
            raise OptimizationRefused(f"{path} lives in {file}, not {change['file']}.")
        if not low <= float(change["to"]) <= high:
            raise OptimizationRefused(f"{path} = {change['to']} is outside the allowed {low:g}-{high:g}.")
        if current.get(path) != change["from"]:
            raise OptimizationRefused(
                f"{path} is now {current.get(path)}, not the {change['from']} this option was proposed "
                "against — the evidence no longer applies. Wait for the next review."
            )
        text = new_text.get(file) or (CONFIG_DIR / file).read_text()
        new_text[file] = set_yaml_scalar(text, path, change["to"])

    if "agent_config.yaml" in new_text:
        weights = yaml.safe_load(new_text["agent_config.yaml"]).get("scoring_weights", {})
        if abs(sum(weights.values()) - 1.0) > 1e-6:
            raise OptimizationRefused(f"Scoring weights would sum to {sum(weights.values()):.4f}, not 1.0.")

    for file, text in new_text.items():
        parsed = yaml.safe_load(text)
        for change in option["changes"]:
            if change["file"] == file and _get_path(parsed, change["path"]) != change["to"]:
                raise OptimizationRefused(f"Editing {file} did not produce {change['path']} = {change['to']}.")
    for file, text in new_text.items():
        (CONFIG_DIR / file).write_text(text)

    record = {
        "option_id": option_id,
        "title": option["title"],
        "changes": option["changes"],
        "loosens_guardrail": bool(option.get("loosens_guardrail")),
        "decided_by": decided_by,
        "applied_at": datetime.now(timezone.utc).isoformat(),
    }
    append_json(OPTIMIZATIONS_DIR / day / "applied.json", record)
    return record


def dismiss_option(option_id: str, decided_by: str = "email-link") -> dict[str, Any]:
    found = find_option(option_id)
    if found is None:
        raise OptimizationRefused(f"No saved option with id {option_id!r} in data/optimizations/.")
    day, option = found
    if option_id in _decided_ids("applied.json") | _decided_ids("dismissed.json"):
        raise OptimizationRefused(f"{option_id} was already applied or dismissed.")
    record = {
        "option_id": option_id,
        "title": option["title"],
        "decided_by": decided_by,
        "dismissed_at": datetime.now(timezone.utc).isoformat(),
    }
    append_json(OPTIMIZATIONS_DIR / day / "dismissed.json", record)
    return record

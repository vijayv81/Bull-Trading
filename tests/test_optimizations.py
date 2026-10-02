import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from trading_agent import config as cfg
from trading_agent import optimizations as opt
from trading_agent.notify import approval_gateway as gw

REAL_CONFIG_DIR = Path(cfg.CONFIG_DIR)


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Copies of the real config files plus an empty data/optimizations, so
    apply_option() edits files that look exactly like production ones."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("agent_config.yaml", "risk_limits.yaml", "watchlist.yaml"):
        shutil.copy(REAL_CONFIG_DIR / name, config_dir / name)
    monkeypatch.setattr(cfg, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(opt, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(opt, "OPTIMIZATIONS_DIR", tmp_path / "optimizations")
    return config_dir


def _review(**overrides):
    base = {
        "window_end": "2026-10-02",
        "lookback_days": 30,
        "signal_hit_rates": {},
        "refusal_breakdown": {},
        "never_decided": [],
        "attempt_log_span": "3 of the last 30 days, since 2026-09-30",
    }
    base.update(overrides)
    return base


def _load(config_dir, name):
    return yaml.safe_load((config_dir / name).read_text())


# --- set_yaml_scalar ------------------------------------------------------------


def test_set_yaml_scalar_changes_only_that_value_and_keeps_comments():
    text = (
        "# top comment\n"
        "execution:\n"
        "  # why the drift limit exists\n"
        "  max_price_drift_pct: 3.0  # trailing note\n"
        "  reverify_technical: true\n"
        "other:\n"
        "  max_price_drift_pct: 9.0\n"
    )
    out = opt.set_yaml_scalar(text, "execution.max_price_drift_pct", 4.0)
    assert out == text.replace("max_price_drift_pct: 3.0  # trailing note", "max_price_drift_pct: 4.0 # trailing note")
    assert yaml.safe_load(out)["other"]["max_price_drift_pct"] == 9.0


def test_set_yaml_scalar_refuses_missing_path_and_mappings():
    text = "execution:\n  max_price_drift_pct: 3.0\n"
    with pytest.raises(opt.OptimizationRefused):
        opt.set_yaml_scalar(text, "execution.nope", 1)
    with pytest.raises(opt.OptimizationRefused):
        opt.set_yaml_scalar(text, "execution", 1)


def test_set_yaml_scalar_on_real_config_files_preserves_everything_else():
    for name, path, value in (
        ("risk_limits.yaml", "execution.max_price_drift_pct", 4.0),
        ("risk_limits.yaml", "operational.approval_expiry_hours", 3),
        ("agent_config.yaml", "scoring_weights.sentiment", 0.2),
    ):
        original = (REAL_CONFIG_DIR / name).read_text()
        out = opt.set_yaml_scalar(original, path, value)
        before, after = yaml.safe_load(original), yaml.safe_load(out)
        section, key = path.split(".")
        assert after[section][key] == value
        after[section][key] = before[section][key]
        assert after == before
        assert sum(1 for a, b in zip(original.splitlines(), out.splitlines()) if a != b) == 1


# --- build_options -------------------------------------------------------------


def test_weight_option_needs_enough_outcomes_and_a_real_gap(sandbox):
    thin = _review(signal_hit_rates={"sentiment": {"hit_rate": 0.3, "n": 3}, "technical": {"hit_rate": 0.9, "n": 3}})
    assert opt.build_options(thin) == []

    review = _review(
        signal_hit_rates={"sentiment": {"hit_rate": 0.35, "n": 12}, "catalyst": {"hit_rate": 0.75, "n": 12}}
    )
    [option] = opt.build_options(review)
    assert option["id"] == "2026-10-02-weights-sentiment-to-catalyst"
    assert option["loosens_guardrail"] is False
    assert option["changes"] == [
        {"file": "agent_config.yaml", "path": "scoring_weights.sentiment", "from": 0.25, "to": 0.2},
        {"file": "agent_config.yaml", "path": "scoring_weights.catalyst", "from": 0.2, "to": 0.25},
    ]


def test_drift_option_needs_mostly_stale_refusals_and_is_flagged_as_loosening(sandbox):
    few = _review(refusal_breakdown={"stale: price drift": 5})
    assert opt.build_options(few) == []
    mostly_other = _review(refusal_breakdown={"stale: price drift": 4, "position size cap": 8})
    assert opt.build_options(mostly_other) == []

    review = _review(refusal_breakdown={"stale: price drift": 24, "stale: technical reversal": 5, "position size cap": 6})
    [option] = opt.build_options(review)
    assert option["changes"] == [
        {"file": "risk_limits.yaml", "path": "execution.max_price_drift_pct", "from": 3.0, "to": 4.0}
    ]
    assert option["loosens_guardrail"] is True
    assert option["risk"]
    assert "29 of 35" in option["why"]


def test_drift_option_never_exceeds_its_ceiling(sandbox, monkeypatch):
    monkeypatch.setattr(opt, "load_risk_limits", lambda: {"execution": {"max_price_drift_pct": 5.0}, "operational": {}})
    assert opt.build_options(_review(refusal_breakdown={"stale: price drift": 20})) == []


def test_expiry_option_when_many_recommendations_expire_undecided(sandbox):
    review = _review(never_decided=[{"ticker": f"T{i}"} for i in range(85)])
    [option] = opt.build_options(review)
    assert option["changes"] == [
        {"file": "risk_limits.yaml", "path": "operational.approval_expiry_hours", "from": 2, "to": 3}
    ]
    assert opt.build_options(_review(never_decided=[{"ticker": "A"}] * 5)) == []


# --- apply / dismiss -------------------------------------------------------------


def _save(options):
    opt.save_options(options, "2026-10-02")


def _drift_option(**overrides):
    option = {
        "id": "2026-10-02-max-price-drift-4",
        "title": "Raise the stale-price limit from 3% to 4%",
        "why": "w",
        "effect": "e",
        "loosens_guardrail": True,
        "changes": [{"file": "risk_limits.yaml", "path": "execution.max_price_drift_pct", "from": 3.0, "to": 4.0}],
    }
    option.update(overrides)
    return option


def test_apply_edits_config_and_records_an_audit_entry(sandbox):
    _save([_drift_option()])
    record = opt.apply_option("2026-10-02-max-price-drift-4", decided_by="email-link", acknowledged_loosening=True)

    assert _load(sandbox, "risk_limits.yaml")["execution"]["max_price_drift_pct"] == 4.0
    assert record["decided_by"] == "email-link"
    applied = json.loads((opt.OPTIMIZATIONS_DIR / "2026-10-02" / "applied.json").read_text())
    assert applied[0]["option_id"] == "2026-10-02-max-price-drift-4"
    assert opt.pending_options() == []


def test_apply_twice_is_refused(sandbox):
    _save([_drift_option()])
    opt.apply_option("2026-10-02-max-price-drift-4", acknowledged_loosening=True)
    with pytest.raises(opt.OptimizationRefused, match="already applied"):
        opt.apply_option("2026-10-02-max-price-drift-4", acknowledged_loosening=True)


def test_apply_refuses_when_config_moved_since_proposal(sandbox):
    _save([_drift_option(changes=[{"file": "risk_limits.yaml", "path": "execution.max_price_drift_pct", "from": 2.0, "to": 4.0}])])
    with pytest.raises(opt.OptimizationRefused, match="evidence no longer applies"):
        opt.apply_option("2026-10-02-max-price-drift-4", acknowledged_loosening=True)
    assert _load(sandbox, "risk_limits.yaml")["execution"]["max_price_drift_pct"] == 3.0


def test_apply_refuses_out_of_bounds_values(sandbox):
    _save([_drift_option(changes=[{"file": "risk_limits.yaml", "path": "execution.max_price_drift_pct", "from": 3.0, "to": 25.0}])])
    with pytest.raises(opt.OptimizationRefused, match="outside the allowed"):
        opt.apply_option("2026-10-02-max-price-drift-4", acknowledged_loosening=True)
    assert _load(sandbox, "risk_limits.yaml")["execution"]["max_price_drift_pct"] == 3.0


def test_apply_refuses_settings_that_are_not_clickable(sandbox):
    _save([_drift_option(changes=[{"file": "risk_limits.yaml", "path": "operational.trading_enabled", "from": True, "to": False}])])
    with pytest.raises(opt.OptimizationRefused, match="not a clickable setting"):
        opt.apply_option("2026-10-02-max-price-drift-4", acknowledged_loosening=True)


def test_apply_refuses_weights_that_would_not_sum_to_one(sandbox):
    _save(
        [
            {
                "id": "lopsided",
                "title": "t",
                "why": "w",
                "effect": "e",
                "changes": [{"file": "agent_config.yaml", "path": "scoring_weights.sentiment", "from": 0.25, "to": 0.2}],
            }
        ]
    )
    with pytest.raises(opt.OptimizationRefused, match="sum to"):
        opt.apply_option("lopsided")
    assert _load(sandbox, "agent_config.yaml")["scoring_weights"]["sentiment"] == 0.25


def test_apply_weight_rebalance_end_to_end(sandbox):
    review = _review(
        signal_hit_rates={"sentiment": {"hit_rate": 0.35, "n": 12}, "catalyst": {"hit_rate": 0.75, "n": 12}}
    )
    _save(opt.build_options(review))
    opt.apply_option("2026-10-02-weights-sentiment-to-catalyst")
    weights = _load(sandbox, "agent_config.yaml")["scoring_weights"]
    assert weights["sentiment"] == 0.2
    assert weights["catalyst"] == 0.25
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_apply_unknown_id_is_refused(sandbox):
    with pytest.raises(opt.OptimizationRefused, match="No saved option"):
        opt.apply_option("made-up")


def test_dismiss_records_and_removes_from_pending(sandbox):
    _save([_drift_option()])
    assert [o["id"] for o in opt.pending_options()] == ["2026-10-02-max-price-drift-4"]
    opt.dismiss_option("2026-10-02-max-price-drift-4")
    assert opt.pending_options() == []
    assert _load(sandbox, "risk_limits.yaml")["execution"]["max_price_drift_pct"] == 3.0


# --- email links -----------------------------------------------------------------


def test_optimization_link_is_none_without_a_ticket_url(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {}})
    assert gw.optimization_link(_drift_option(), "apply") is None


def test_optimization_link_is_a_short_plain_anchor(monkeypatch):
    """The viewer only passes a plain #anchor (letters, digits, . _ ~ -)
    through to the page; a first version that packed the whole option into
    a ~900-character fragment arrived empty."""
    monkeypatch.setattr(
        gw, "load_agent_config", lambda: {"notifications": {"optimization_ticket_artifact_url": "https://claude.ai/artifact/X"}}
    )
    loosening = gw.optimization_link(_drift_option(), "apply")
    assert loosening == "https://claude.ai/artifact/X#2026-10-02-max-price-drift-4.apply.loosens"
    safe = gw.optimization_link(_drift_option(id="2026-10-02-weights-a-to-b", loosens_guardrail=False), "dismiss")
    assert safe == "https://claude.ai/artifact/X#2026-10-02-weights-a-to-b.dismiss"
    anchor = loosening.split("#", 1)[1]
    assert re.fullmatch(r"[A-Za-z0-9._~-]+", anchor)
    assert len(anchor) < 100


def test_option_ids_only_use_anchor_safe_characters(sandbox):
    review = _review(
        signal_hit_rates={"sentiment": {"hit_rate": 0.35, "n": 12}, "historical_hitrate": {"hit_rate": 0.8, "n": 12}},
        refusal_breakdown={"stale: price drift": 24},
        never_decided=[{}] * 20,
    )
    for option in opt.build_options(review):
        assert re.fullmatch(r"[A-Za-z0-9._~-]+", option["id"]), option["id"]


def _email_review(options):
    return {
        "window_start": "2026-09-03",
        "window_end": "2026-10-02",
        "lookback_days": 30,
        "trades_count": 0,
        "realized_pnl": {"total": 0.0, "by_symbol": {}, "pending_fills": 0},
        "unrealized_pnl": {"positions": [], "total": 0.0, "error": None},
        "portfolio_status": {"equity": 1.0, "cash": 1.0, "buying_power": 1.0, "error": None},
        "actionable_count": 0,
        "executed_count": 0,
        "never_decided": [],
        "approved_not_executed": [],
        "rejected": [],
        "auto_apply_attempts_by_status": {},
        "refusal_breakdown": {},
        "missed_opportunities": [],
        "signal_hit_rates": {},
        "optimization_proposals": [],
        "scoring_weights": {},
        "learning_outcome": None,
        "config_tuning_notes": [],
        "optimization_options": options,
    }


def test_email_leads_with_clickable_apply_and_dismiss_buttons(monkeypatch):
    monkeypatch.setattr(
        gw,
        "load_agent_config",
        lambda: {"notifications": {"channel": ["email"], "optimization_ticket_artifact_url": "https://claude.ai/artifact/X"}},
    )
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_weekly_learning_review(_email_review([_drift_option(risk="r")]))

    subject, plain, html_body = calls[0]
    assert subject.endswith("1 optimization(s) to review")
    assert "Optimizations you can apply:" in plain
    assert "Review & apply (asks you to confirm): https://claude.ai/artifact/X#" in plain
    assert "LOOSENS A GUARDRAIL" in plain
    assert "Review &amp; apply (loosens a guardrail)" in html_body and "Dismiss" in html_body
    assert html_body.index("Optimizations you can apply") < html_body.index("Portfolio status")


def test_email_falls_back_to_the_cli_command_without_a_ticket_url(monkeypatch):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_weekly_learning_review(_email_review([_drift_option()]))

    _, plain, html_body = calls[0]
    assert "trading-agent optimizations apply 2026-10-02-max-price-drift-4 --acknowledge-loosening" in plain
    assert "Apply this change" not in html_body


def test_loosening_option_needs_an_explicit_acknowledgment(sandbox):
    _save([_drift_option()])
    with pytest.raises(opt.OptimizationRefused, match="needs an explicit acknowledgment"):
        opt.apply_option("2026-10-02-max-price-drift-4")
    assert _load(sandbox, "risk_limits.yaml")["execution"]["max_price_drift_pct"] == 3.0
    record = opt.apply_option("2026-10-02-max-price-drift-4", acknowledged_loosening=True)
    assert record["loosens_guardrail"] is True


def test_non_loosening_option_applies_in_one_step(sandbox):
    review = _review(
        signal_hit_rates={"sentiment": {"hit_rate": 0.35, "n": 12}, "catalyst": {"hit_rate": 0.75, "n": 12}}
    )
    _save(opt.build_options(review))
    opt.apply_option("2026-10-02-weights-sentiment-to-catalyst")  # no acknowledgment needed


def test_dismiss_twice_or_after_apply_is_refused(sandbox):
    _save([_drift_option()])
    opt.dismiss_option("2026-10-02-max-price-drift-4")
    with pytest.raises(opt.OptimizationRefused, match="already applied or dismissed"):
        opt.dismiss_option("2026-10-02-max-price-drift-4")


def test_email_one_click_label_for_options_that_do_not_loosen(monkeypatch):
    monkeypatch.setattr(
        gw,
        "load_agent_config",
        lambda: {"notifications": {"channel": ["email"], "optimization_ticket_artifact_url": "https://claude.ai/artifact/X"}},
    )
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    safe = _drift_option(id="w", loosens_guardrail=False)
    gw.notify_weekly_learning_review(_email_review([safe]))

    _, plain, html_body = calls[0]
    assert "    Apply: https://claude.ai/artifact/X#" in plain
    assert "Apply this change" in html_body
    assert "loosens a guardrail)" not in html_body

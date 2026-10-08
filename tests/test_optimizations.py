import json
from datetime import datetime, timedelta, timezone
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
    # The tunable values these tests are written against. The live config
    # moves as options get applied (max_price_drift_pct went 3.0 -> 4.0 on
    # 2026-10-05), so pin them here instead of inheriting whatever it holds.
    for file, path, value in (
        ("risk_limits.yaml", "execution.max_price_drift_pct", 3.0),
        ("risk_limits.yaml", "operational.approval_expiry_hours", 2),
        ("agent_config.yaml", "scoring_weights.sentiment", 0.25),
        ("agent_config.yaml", "scoring_weights.technical", 0.30),
        ("agent_config.yaml", "scoring_weights.fundamental", 0.15),
        ("agent_config.yaml", "scoring_weights.catalyst", 0.20),
        ("agent_config.yaml", "scoring_weights.historical_hitrate", 0.10),
    ):
        target = config_dir / file
        target.write_text(opt.set_yaml_scalar(target.read_text(), path, value))
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
        # Values the live config won't plausibly already hold, so exactly one
        # line has to change whatever options have been applied so far.
        ("risk_limits.yaml", "execution.max_price_drift_pct", 2.5),
        ("risk_limits.yaml", "operational.approval_expiry_hours", 5),
        ("agent_config.yaml", "scoring_weights.sentiment", 0.17),
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


# --- weekly report's improvements section ------------------------------------------


def _ticket(monkeypatch, url="https://claude.ai/artifact/X"):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"optimization_ticket_artifact_url": url}})


def test_weekly_report_section_lists_open_options_with_clickable_links(sandbox, monkeypatch):
    from trading_agent.reporting import report_builder as rb

    _ticket(monkeypatch)
    _save([_drift_option()])
    [line] = rb._open_option_lines()
    assert "**Raise the stale-price limit from 3% to 4%** (loosens a guardrail)" in line
    assert "`execution.max_price_drift_pct` 3.0 → 4.0" in line
    assert "[Review & apply](https://claude.ai/artifact/X#2026-10-02-max-price-drift-4.apply.loosens)" in line
    assert "[Dismiss](https://claude.ai/artifact/X#2026-10-02-max-price-drift-4.dismiss.loosens)" in line


def test_weekly_report_section_skips_decided_and_outdated_options(sandbox, monkeypatch):
    from trading_agent.reporting import report_builder as rb

    _ticket(monkeypatch)
    outdated = _drift_option(
        id="old", changes=[{"file": "risk_limits.yaml", "path": "execution.max_price_drift_pct", "from": 2.0, "to": 3.0}]
    )
    dismissed = _drift_option(id="dismissed")
    _save([outdated, dismissed])
    opt.dismiss_option("dismissed")
    assert rb._open_option_lines() == [
        "_No open options right now. The Saturday weekly learning review proposes new ones, "
        "each with Apply/Dismiss links._"
    ]


def test_weekly_report_section_falls_back_to_the_command(sandbox, monkeypatch):
    from trading_agent.reporting import report_builder as rb

    _ticket(monkeypatch, url=None)
    _save([_drift_option()])
    [line] = rb._open_option_lines()
    assert "`trading-agent optimizations apply 2026-10-02-max-price-drift-4 --acknowledge-loosening`" in line


# --- daily proposal (2026-10-05) ----------------------------------------------

DAILY_RATES = {
    "technical": {"hit_rate": 0.7, "n": 10},
    "catalyst": {"hit_rate": 0.3, "n": 10},
    "sentiment": {"hit_rate": 0.5, "n": 6},
}


def test_propose_daily_saves_the_weight_shift_as_an_applicable_option(sandbox):
    [option] = opt.propose_daily("2026-10-05", DAILY_RATES)
    assert option["id"] == "2026-10-05-weights-catalyst-to-technical"
    assert option["changes"] == [
        {"file": "agent_config.yaml", "path": "scoring_weights.catalyst", "from": 0.2, "to": 0.15},
        {"file": "agent_config.yaml", "path": "scoring_weights.technical", "from": 0.3, "to": 0.35},
    ]
    assert not option["loosens_guardrail"]
    assert [o["id"] for o in opt.options_proposed_on("2026-10-05")] == [option["id"]]

    opt.apply_option(option["id"], decided_by="daily-pr")
    weights = _load(sandbox, "agent_config.yaml")["scoring_weights"]
    assert weights["catalyst"] == 0.15 and weights["technical"] == 0.35
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_propose_daily_needs_the_same_evidence_as_the_weekly_review(sandbox):
    thin = {"technical": {"hit_rate": 0.7, "n": 5}, "catalyst": {"hit_rate": 0.3, "n": 5}}
    assert opt.propose_daily("2026-10-05", thin) == []
    close = {"technical": {"hit_rate": 0.55, "n": 12}, "catalyst": {"hit_rate": 0.45, "n": 12}}
    assert opt.propose_daily("2026-10-05", close) == []


def test_propose_daily_never_offers_the_weekly_only_options(sandbox):
    # Only signal_hit_rates go in, so the refusal/expiry options can't appear.
    assert [o["id"] for o in opt.propose_daily("2026-10-05", DAILY_RATES)] == [
        "2026-10-05-weights-catalyst-to-technical"
    ]


def test_an_identical_pending_option_with_a_pr_is_not_proposed_again(sandbox):
    [option] = opt.propose_daily("2026-10-05", DAILY_RATES)
    opt.record_pr(option["id"], "https://github.com/x/y/pull/7")
    assert opt.propose_daily("2026-10-06", DAILY_RATES) == []  # its PR is still waiting
    # ...and that PR keeps being offered for review instead of vanishing from the email.
    [waiting] = opt.options_awaiting_review("2026-10-06")
    assert waiting["id"] == option["id"] and waiting["pr_url"] == "https://github.com/x/y/pull/7"


def test_an_identical_pending_option_without_a_pr_is_returned_for_one_not_duplicated(sandbox):
    [first] = opt.propose_daily("2026-10-05", DAILY_RATES)  # the PR step then failed: nothing recorded
    [again] = opt.propose_daily("2026-10-06", DAILY_RATES)
    assert again["id"] == first["id"]
    assert len(opt.pending_options()) == 1


def test_a_weight_shift_right_after_another_is_still_proposed_with_a_caution(sandbox):
    [option] = opt.propose_daily("2026-10-05", DAILY_RATES)
    opt.apply_option(option["id"], decided_by="daily-pr")
    # Next morning the rolling hit rates say the same thing about the new weights
    # (catalyst 0.15, technical 0.35). It's still offered, so the email always has
    # something to act on, but it says the weights were just changed.
    [again] = opt.propose_daily("2026-10-06", DAILY_RATES)
    assert "last changed on" in again["risk"] and "0 day(s) ago" in again["risk"]
    assert option["title"] in again["risk"] and "consider waiting" in again["risk"]
    assert again["changes"][0]["from"] == 0.15
    # The weekly review's weight option gets the same caution.
    [weekly] = opt.build_options(_review(signal_hit_rates=DAILY_RATES))
    assert "last changed on" in weekly["risk"]


def test_no_caution_when_weights_have_not_changed_recently(sandbox):
    [option] = opt.propose_daily("2026-10-05", DAILY_RATES)
    assert "risk" not in option


def test_the_recent_change_caution_expires(sandbox):
    [option] = opt.propose_daily("2026-10-05", DAILY_RATES)
    opt.apply_option(option["id"], decided_by="daily-pr")
    later = datetime.now(timezone.utc) + timedelta(days=opt.WEIGHT_CAUTION_DAYS + 1)
    assert opt._last_weight_change(opt.WEIGHT_CAUTION_DAYS, now=later) is None
    assert opt._recent_change_caution(now=later) is None
    assert opt._last_weight_change(opt.WEIGHT_CAUTION_DAYS)["option_id"] == option["id"]


def test_record_pr_replaces_an_earlier_url_and_refuses_unknown_options(sandbox):
    [option] = opt.propose_daily("2026-10-05", DAILY_RATES)
    opt.record_pr(option["id"], "https://x/pull/1")
    opt.record_pr(option["id"], "https://x/pull/2")
    assert opt.pr_url_for(option["id"]) == "https://x/pull/2"
    assert len(json.loads((opt.OPTIMIZATIONS_DIR / "2026-10-05" / "prs.json").read_text())) == 1
    with pytest.raises(opt.OptimizationRefused):
        opt.record_pr("no-such-option", "https://x/pull/3")
    assert opt.pr_url_for("no-such-option") is None


def test_options_awaiting_review_drop_out_when_merged_or_old(sandbox):
    [option] = opt.propose_daily("2026-10-05", DAILY_RATES)
    opt.record_pr(option["id"], "https://x/pull/1")
    assert [o["id"] for o in opt.options_awaiting_review("2026-10-05")] == [option["id"]]
    # too old to keep reminding about
    assert opt.options_awaiting_review("2026-10-05"[:8] + "20") == []
    # merged: the config moved, so it is no longer open
    opt.apply_option(option["id"], decided_by="daily-pr")
    assert opt.options_awaiting_review("2026-10-06") == []


def test_options_without_a_recorded_pr_are_never_offered_for_review(sandbox):
    opt.propose_daily("2026-10-05", DAILY_RATES)
    assert opt.options_awaiting_review("2026-10-05") == []


def test_add_options_keeps_what_the_weekly_review_already_saved_for_that_day(sandbox):
    opt.save_options([{"id": "weekly-one", "changes": []}], "2026-10-05")
    opt.add_options([{"id": "daily-one", "changes": []}, {"id": "weekly-one", "changes": []}], "2026-10-05")
    saved = json.loads((opt.OPTIMIZATIONS_DIR / "2026-10-05" / "options.json").read_text())
    assert [o["id"] for o in saved] == ["weekly-one", "daily-one"]


# --- daily email PR section ----------------------------------------------------

DAILY_PR = "https://github.com/vijayv81/Bull-Trading/pull/77"


def _daily_summary(**overrides):
    base = {
        "day": "2026-10-05", "portfolio_return_pct": 1.0, "benchmark_symbol": "SPY",
        "benchmark_return_pct": 0.5, "outperformance_pct": 0.5, "recommendations_count": 3,
        "trades_count": 1, "journal_entries": [],
        "optimization_proposals": ["catalyst trails technical"], "optimization_options": [],
    }
    base.update(overrides)
    return base


def _send_daily(monkeypatch, summary, **kwargs):
    monkeypatch.setattr(gw, "load_agent_config", lambda: {"notifications": {"channel": ["email"]}})
    calls = []
    monkeypatch.setattr("trading_agent.notify.senders.send_email", lambda *a, **k: calls.append(a))
    gw.notify_daily_summary(summary, **kwargs)
    return calls[0]


def test_daily_email_links_the_pr_with_the_change_details(monkeypatch):
    option = {
        "id": "o", "title": "Shift 0.05 of scoring weight from catalyst to technical",
        "why": "catalyst agreed 30% over 10 vs technical 70% over 10.",
        "effect": "Confidence leans more on technical.",
        "changes": [{"file": "agent_config.yaml", "path": "scoring_weights.catalyst", "from": 0.2, "to": 0.15}],
    }
    subject, plain, html = _send_daily(monkeypatch, _daily_summary(optimization_options=[option]), pr_url=DAILY_PR)
    assert DAILY_PR in plain
    assert "scoring_weights.catalyst: 0.2 -> 0.15" in plain
    assert "Merging applies the change to main" in plain
    assert f'href="{DAILY_PR}"' in html
    assert "scoring_weights.catalyst: 0.2 -&gt; 0.15" in html or "scoring_weights.catalyst: 0.2 -> 0.15" in html
    assert "change to approve" in subject
    assert "Proposals are never applied automatically — incorporating" not in plain


def test_daily_email_is_unchanged_without_a_pr(monkeypatch):
    subject, plain, html = _send_daily(monkeypatch, _daily_summary())
    assert "Review & approve" not in plain and "change to approve" not in subject
    assert "Proposals are never applied automatically — incorporating" in plain


def test_daily_email_ignores_a_pr_url_when_there_is_no_option(monkeypatch):
    subject, plain, html = _send_daily(monkeypatch, _daily_summary(), pr_url=DAILY_PR)
    assert DAILY_PR not in plain and DAILY_PR not in html


def test_daily_email_links_each_options_own_pr_without_a_pr_url_argument(monkeypatch):
    option = {
        "id": "o", "title": "Shift 0.05 of scoring weight from sentiment to technical",
        "why": "sentiment 29% over 14 vs technical 50% over 20.", "effect": "Leans on technical.",
        "risk": "Scoring weights were last changed on 2026-10-05 (3 day(s) ago): x. Consider waiting.",
        "changes": [{"file": "agent_config.yaml", "path": "scoring_weights.sentiment", "from": 0.25, "to": 0.2}],
        "pr_url": "https://github.com/vijayv81/Bull-Trading/pull/61",
    }
    subject, plain, html = _send_daily(monkeypatch, _daily_summary(optimization_options=[option]))
    assert "https://github.com/vijayv81/Bull-Trading/pull/61" in plain
    assert "Heads-up: Scoring weights were last changed on 2026-10-05" in plain
    assert 'href="https://github.com/vijayv81/Bull-Trading/pull/61"' in html and "Heads-up" in html
    assert "change to approve" in subject


def test_daily_email_says_when_a_proposal_has_no_pr(monkeypatch):
    subject, plain, html = _send_daily(monkeypatch, _daily_summary())  # a proposal, but no option/PR
    assert "No pull request is open for this proposal yet" in plain
    assert "No pull request is open for this proposal yet" in html
    assert "change to approve" not in subject


def test_daily_email_has_no_missing_pr_line_when_there_is_no_proposal(monkeypatch):
    subject, plain, html = _send_daily(monkeypatch, _daily_summary(optimization_proposals=[]))
    assert "No pull request is open" not in plain and "No pull request is open" not in html

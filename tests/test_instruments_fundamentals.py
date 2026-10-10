"""Fundamental score, blank-check handling, warrant block, hit-rate minimum."""

import json

import pytest

from trading_agent import guardrails as g
from trading_agent.scoring import fundamentals as f
from trading_agent.scoring import instruments as ins
from trading_agent.scoring import recommendation_engine as engine

FCFG = {
    "enabled": True, "min_metrics": 2, "min_revenue": 1_000_000, "min_market_cap": 10_000_000,
    "ranges": {
        "profit_margin": [-0.2, 0.2], "revenue_growth": [-0.2, 0.4], "return_on_equity": [-0.2, 0.3],
        "debt_to_equity": [0, 200], "pe": [15, 60],
    },
}
BCFG = {"trust_value": 10.0, "max_premium_to_trust_pct": 15.0, "max_position_pct_of_portfolio": 1.0,
        "block_warrants_and_rights": True}


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(ins, "load_risk_limits", lambda: {"blank_check": BCFG, "position": {"max_position_pct_of_portfolio": 5.0}})
    monkeypatch.setattr(g, "load_risk_limits", lambda: {"blank_check": BCFG, "position": {"max_position_pct_of_portfolio": 5.0}})


def test_classes_from_symbol():
    assert ins.instrument_class("FVNNU") == ins.BLANK_CHECK_UNIT
    assert ins.instrument_class("GRMLW") == ins.WARRANT_OR_RIGHT
    assert ins.instrument_class("ABCDR") == ins.WARRANT_OR_RIGHT
    assert ins.instrument_class("TSLA") == ins.OPERATING and ins.instrument_class("GOOGL") == ins.OPERATING


def test_override_wins(monkeypatch):
    monkeypatch.setattr(ins, "load_risk_limits", lambda: {"position": {"instrument_overrides": {"FVNNU": "operating"}}})
    assert ins.instrument_class("FVNNU") == ins.OPERATING


def test_blank_check_score_follows_premium_to_trust():
    assert ins.blank_check_score(9.9, BCFG) == 0.65
    assert ins.blank_check_score(10.0 * 1.075, BCFG) == pytest.approx(0.375, abs=1e-3)
    assert ins.blank_check_score(32.0, BCFG) == 0.10
    assert ins.blank_check_score(None, BCFG) is None


def test_fundamental_score_for_a_real_company_and_shells():
    healthy = {"profitMargins": 0.2, "revenueGrowth": 0.4, "returnOnEquity": 0.3, "debtToEquity": 0,
               "forwardPE": 15, "totalRevenue": 5e9, "marketCap": 5e10}
    weak = {"profitMargins": -0.2, "revenueGrowth": -0.2, "returnOnEquity": -0.2, "debtToEquity": 200,
            "forwardPE": 60, "totalRevenue": 5e9, "marketCap": 5e10}
    assert f.fundamental_score(healthy, FCFG) == 1.0
    assert f.fundamental_score(weak, FCFG) == 0.0
    shell = {"profitMargins": 0.0, "returnOnEquity": 3.9, "totalRevenue": None, "marketCap": None}
    assert f.fundamental_score(shell, FCFG) is None
    assert f.fundamental_score(None, FCFG) is None
    assert f.fundamental_score({"profitMargins": 0.1, "totalRevenue": 5e9, "marketCap": 5e10}, FCFG) is None  # <2 readings
    assert f.fundamental_score(healthy, {**FCFG, "enabled": False}) is None


def test_warrants_and_overpriced_units_cannot_be_bought_but_can_be_sold():
    assert "warrant" in g.instrument_reason("GRMLW", "BUY", 0.5)
    assert g.instrument_reason("GRMLW", "SELL", 0.5) is None
    assert "trust value" in g.instrument_reason("FVNNU", "BUY", 32.0)
    assert g.instrument_reason("FVNNU", "BUY", 10.1) is None
    assert g.instrument_reason("TSLA", "BUY", 400.0) is None


def test_unit_with_unreadable_price_fails_closed(monkeypatch):
    monkeypatch.setattr(g, "_mid_price", lambda s: (_ for _ in ()).throw(ValueError("no quote")))
    assert "refusing" in g.instrument_reason("FVNNU", "BUY")


def test_units_get_their_own_lower_position_cap(monkeypatch):
    monkeypatch.setattr(g, "_account", lambda: {"equity": "10000"})
    monkeypatch.setattr(g, "_existing_position_value", lambda s: 0.0)
    assert g.position_size_reason("FVNNU", "BUY", 20, price=10.0) is not None  # 2% > 1% unit cap
    assert g.position_size_reason("TSLA", "BUY", 20, price=10.0) is None  # 2% < 5%


def test_hit_rate_needs_a_minimum_sample(monkeypatch, tmp_path):
    (tmp_path / "strategy_metrics.json").write_text(json.dumps(
        {"by_ticker": {"FEW": {"hit_rate": 1.0, "n": 2}, "MANY": {"hit_rate": 0.6, "n": 5}}}
    ))
    monkeypatch.setattr(engine, "PERFORMANCE_DIR", tmp_path)
    assert engine.historical_hitrate("FEW") is None
    assert engine.historical_hitrate("MANY") == 0.6
    assert engine.historical_hitrate("NEW") is None

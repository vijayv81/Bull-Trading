import pandas as pd
import pytest

from trading_agent.scoring import recommendation_engine as engine


def _uptrend(n=60, start=100.0, step=1.0) -> pd.DataFrame:
    return pd.DataFrame({"close": [start + i * step for i in range(n)]})


def test_technical_score_uptrend_scores_above_neutral():
    assert engine.technical_score(_uptrend(), fast=5, slow=20) > 0.5


def test_technical_score_insufficient_history_is_neutral():
    assert engine.technical_score(_uptrend(n=10), fast=5, slow=20) == 0.5


def test_technical_score_empty_frame_is_neutral():
    assert engine.technical_score(pd.DataFrame()) == 0.5


@pytest.fixture
def fixed_config(monkeypatch):
    monkeypatch.setattr(
        engine,
        "load_agent_config",
        lambda: {
            "scoring_weights": {
                "sentiment": 0.25,
                "technical": 0.30,
                "fundamental": 0.15,
                "catalyst": 0.20,
                "historical_hitrate": 0.10,
            }
        },
    )
    monkeypatch.setattr(
        engine,
        "load_risk_limits",
        lambda: {"position": {"min_confidence_to_notify": 50, "max_position_pct_of_portfolio": 5.0}},
    )
    monkeypatch.setattr(engine, "historical_hitrate", lambda ticker: 0.5)


def test_score_candidate_high_inputs_yields_buy(fixed_config):
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.9, technical=0.9, fundamental=0.8, catalyst=0.9
    )
    assert rec["action"] == "BUY"
    assert rec["confidence"] > 50
    assert set(rec["component_scores"]) == {
        "sentiment", "technical", "fundamental", "catalyst", "historical_hitrate",
    }


def test_score_candidate_low_inputs_yields_hold(fixed_config):
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.1, technical=0.2, fundamental=0.1, catalyst=0.1
    )
    assert rec["action"] == "HOLD"
    assert rec["suggested_size_pct_of_portfolio"] == 0.0


def test_missing_components_are_excluded_not_defaulted_to_neutral(monkeypatch, fixed_config):
    # historical_hitrate mocked to None (no data yet) by the override below,
    # fundamental passed as None (no vendor) — both should drop out of the sum
    # entirely rather than being treated as a diluting neutral 0.5.
    monkeypatch.setattr(engine, "historical_hitrate", lambda ticker: None)
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=0.8, fundamental=None, catalyst=0.8
    )
    # Only sentiment(0.25) + technical(0.30) + catalyst(0.20) remain, renormalized
    # to sum to 1: (0.25+0.30+0.20)/0.75 each contribute their share of 0.8*100=80.
    assert rec["confidence"] == pytest.approx(80.0)
    assert rec["component_scores"]["fundamental"] is None
    assert rec["component_scores"]["historical_hitrate"] is None


def test_partial_missing_component_still_discriminates(monkeypatch, fixed_config):
    monkeypatch.setattr(engine, "historical_hitrate", lambda ticker: None)
    high = engine.score_candidate(
        "TSLA", "midday", sentiment=0.9, technical=0.9, fundamental=None, catalyst=0.9
    )
    low = engine.score_candidate(
        "GOOG", "midday", sentiment=0.2, technical=0.2, fundamental=None, catalyst=0.2
    )
    assert high["confidence"] > low["confidence"]


def test_no_components_available_raises(monkeypatch, fixed_config):
    monkeypatch.setattr(engine, "historical_hitrate", lambda ticker: None)
    with pytest.raises(ValueError):
        engine.score_candidate(
            "TSLA", "midday", sentiment=None, technical=None, fundamental=None, catalyst=None
        )


# --- confidence-scaled position sizing ---------------------------------------


@pytest.fixture
def scaled_sizing_config(monkeypatch):
    monkeypatch.setattr(
        engine,
        "load_risk_limits",
        lambda: {
            "position": {
                "min_confidence_to_notify": 50,
                "max_position_pct_of_portfolio": 5.0,
                "min_position_pct_of_portfolio": 1.0,
                "confidence_scaled_sizing": True,
            }
        },
    )


def test_hold_always_sizes_zero(scaled_sizing_config):
    assert engine._suggested_size_pct("HOLD", 90, engine.load_risk_limits()["position"]) == 0.0


def test_sizing_at_threshold_confidence_is_the_floor(scaled_sizing_config):
    risk = engine.load_risk_limits()["position"]
    assert engine._suggested_size_pct("BUY", 50, risk) == 1.0


def test_sizing_at_max_confidence_is_the_cap(scaled_sizing_config):
    risk = engine.load_risk_limits()["position"]
    assert engine._suggested_size_pct("BUY", 100, risk) == 5.0


def test_sizing_scales_between_floor_and_cap(scaled_sizing_config):
    risk = engine.load_risk_limits()["position"]
    low = engine._suggested_size_pct("BUY", 60, risk)
    high = engine._suggested_size_pct("BUY", 90, risk)
    assert 1.0 < low < high < 5.0


def test_sizing_disabled_reverts_to_flat_cap(monkeypatch):
    monkeypatch.setattr(
        engine,
        "load_risk_limits",
        lambda: {
            "position": {
                "min_confidence_to_notify": 50,
                "max_position_pct_of_portfolio": 5.0,
                "min_position_pct_of_portfolio": 1.0,
                "confidence_scaled_sizing": False,
            }
        },
    )
    risk = engine.load_risk_limits()["position"]
    assert engine._suggested_size_pct("BUY", 51, risk) == 5.0
    assert engine._suggested_size_pct("BUY", 100, risk) == 5.0


def test_score_candidate_wires_scaled_size_end_to_end(monkeypatch, scaled_sizing_config):
    monkeypatch.setattr(
        engine,
        "load_agent_config",
        lambda: {
            "scoring_weights": {
                "sentiment": 0.25, "technical": 0.30, "fundamental": 0.15,
                "catalyst": 0.20, "historical_hitrate": 0.10,
            }
        },
    )
    monkeypatch.setattr(engine, "historical_hitrate", lambda ticker: None)
    barely_actionable = engine.score_candidate(
        "TSLA", "midday", sentiment=0.51, technical=0.51, fundamental=None, catalyst=0.51
    )
    strong = engine.score_candidate(
        "TSLA", "midday", sentiment=0.99, technical=0.99, fundamental=None, catalyst=0.99
    )
    assert barely_actionable["action"] == "BUY"
    assert 1.0 <= barely_actionable["suggested_size_pct_of_portfolio"] < strong["suggested_size_pct_of_portfolio"]
    assert strong["suggested_size_pct_of_portfolio"] <= 5.0

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
        lambda: {"position": {"min_confidence_to_notify": 50}},
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

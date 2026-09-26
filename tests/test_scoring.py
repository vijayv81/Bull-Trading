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


def test_technical_score_caps_momentum_for_an_already_extended_move():
    """A stock already up 30% in the last 5 days shouldn't score MORE
    bullish for it than one up exactly at the 10% cap — chasing an extended
    move is a real risk, not a stronger signal (mitigates buying into
    top-gainer movers purely because they already ran).

    fast == slow makes the spread (trend) term exactly 0 for every row,
    isolating the momentum term this test is actually about.
    """
    base = [100.0] * 55
    modest = pd.DataFrame({"close": base + [100.0, 100.0, 100.0, 100.0, 103.0]})  # +3%, under the cap
    at_cap = pd.DataFrame({"close": base + [100.0, 100.0, 100.0, 100.0, 110.0]})  # +10%, right at the cap
    way_past_cap = pd.DataFrame({"close": base + [100.0, 100.0, 100.0, 100.0, 130.0]})  # +30%, well past it

    score_modest = engine.technical_score(modest, fast=20, slow=20)
    score_at_cap = engine.technical_score(at_cap, fast=20, slow=20)
    score_way_past = engine.technical_score(way_past_cap, fast=20, slow=20)

    assert score_at_cap == pytest.approx(score_way_past)  # saturated — no extra credit past the cap
    assert score_modest < score_at_cap <= 1.0  # still rewards a real, moderate move


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


def test_missing_technical_is_excluded_not_defaulted_to_neutral(monkeypatch, fixed_config):
    # technical=None (e.g. not enough bars — see MIN_BARS_FOR_TECHNICAL) must drop
    # out of the weighted sum exactly like fundamental=None/historical_hitrate=None,
    # not silently act like every ticker had a real, identical neutral technical.
    monkeypatch.setattr(engine, "historical_hitrate", lambda ticker: None)
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=None, fundamental=0.8, catalyst=0.8
    )
    assert rec["component_scores"]["technical"] is None
    # Only sentiment(0.25) + fundamental(0.15) + catalyst(0.20) remain, renormalized.
    assert rec["confidence"] == pytest.approx(80.0)


def test_missing_technical_forces_hold_even_at_high_confidence(monkeypatch, fixed_config):
    # technical is the formula's only directional input (BUY vs SELL). With no
    # technical signal there is no basis to guess a direction, so action must be
    # HOLD regardless of how high confidence is from the other components alone.
    monkeypatch.setattr(engine, "historical_hitrate", lambda ticker: None)
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.95, technical=None, fundamental=0.95, catalyst=0.95
    )
    assert rec["confidence"] > 90
    assert rec["action"] == "HOLD"
    assert rec["suggested_size_pct_of_portfolio"] == 0.0


def test_flat_technical_and_flat_proxies_reproduce_the_reported_incident(monkeypatch, fixed_config):
    # Regression test for the production incident: sentiment=1.0 (sources present),
    # catalyst=0.7 (sources present), technical=0.5 (insufficient bars, pre-fix
    # fallback) produced an identical 72.0 confidence for every ticker — fundamental
    # and historical_hitrate were both None in production too (no vendor wired up,
    # no journal history yet), which is what this test reproduces. This proves the
    # exact arithmetic that made every one of 15 tickers score 72 — the real fix is
    # that technical=0.5 should never have been possible in the first place (see
    # orchestrator.py / MIN_BARS_FOR_TECHNICAL), verified in test_orchestrator.py.
    monkeypatch.setattr(engine, "historical_hitrate", lambda ticker: None)
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=1.0, technical=0.5, fundamental=None, catalyst=0.7
    )
    assert rec["confidence"] == pytest.approx(72.0)


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


# --- position_sell_pressure ---------------------------------------------------


def test_sell_pressure_zero_well_within_range():
    assert engine.position_sell_pressure(0.0, stop_loss_pct=4.0, take_profit_pct=8.0) == 0.0
    assert engine.position_sell_pressure(-2.0, stop_loss_pct=4.0, take_profit_pct=8.0) == 0.0
    assert engine.position_sell_pressure(5.0, stop_loss_pct=4.0, take_profit_pct=8.0) == 0.0


def test_sell_pressure_half_right_at_stop_loss():
    assert engine.position_sell_pressure(-4.0, stop_loss_pct=4.0, take_profit_pct=8.0) == pytest.approx(0.5)


def test_sell_pressure_maxes_out_twice_past_stop_loss():
    assert engine.position_sell_pressure(-8.0, stop_loss_pct=4.0, take_profit_pct=8.0) == pytest.approx(1.0)
    assert engine.position_sell_pressure(-20.0, stop_loss_pct=4.0, take_profit_pct=8.0) == pytest.approx(1.0)


def test_sell_pressure_half_right_at_take_profit():
    assert engine.position_sell_pressure(8.0, stop_loss_pct=4.0, take_profit_pct=8.0) == pytest.approx(0.5)


def test_sell_pressure_maxes_out_twice_past_take_profit():
    assert engine.position_sell_pressure(16.0, stop_loss_pct=4.0, take_profit_pct=8.0) == pytest.approx(1.0)


# --- score_candidate: position-pnl bias toward SELL -----------------------------


def test_position_pnl_none_when_not_held(fixed_config):
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=0.8, fundamental=0.8, catalyst=0.8
    )
    assert rec["position_pnl_pct"] is None
    assert rec["sell_pressure"] == 0.0
    assert rec["action"] == "BUY"


def test_position_well_within_range_does_not_change_action(fixed_config):
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=0.9, fundamental=0.8, catalyst=0.8,
        position_pnl_pct=-1.0,
    )
    assert rec["sell_pressure"] == 0.0
    assert rec["action"] == "BUY"


def test_moderate_technical_flips_to_sell_at_stop_loss(fixed_config):
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=0.6, fundamental=0.8, catalyst=0.8,
        position_pnl_pct=-4.0,  # exactly at the stop-loss line -> sell_pressure 0.5
    )
    assert rec["sell_pressure"] == pytest.approx(0.5)
    assert rec["action"] == "SELL"  # 0.6 * 0.5 = 0.3, below the 0.5 direction threshold


def test_max_bullish_technical_survives_right_at_stop_loss(fixed_config):
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=1.0, fundamental=0.8, catalyst=0.8,
        position_pnl_pct=-4.0,
    )
    assert rec["action"] == "BUY"  # 1.0 * 0.5 = 0.5, right at the direction boundary


def test_take_profit_biases_toward_sell_not_buy(fixed_config):
    # Deeply past take-profit (a winning position) still biases toward SELL
    # (lock in gains), never toward BUYing more — sell_pressure only ever
    # pulls the effective technical DOWN, regardless of which threshold fired.
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=0.55, fundamental=0.8, catalyst=0.8,
        position_pnl_pct=20.0,  # well past an 8% take-profit
    )
    assert rec["sell_pressure"] == pytest.approx(1.0)
    assert rec["action"] == "SELL"


def test_mandatory_stop_loss_false_reverts_to_pure_technical(monkeypatch, fixed_config):
    monkeypatch.setattr(
        engine,
        "load_risk_limits",
        lambda: {
            "position": {
                "min_confidence_to_notify": 50,
                "max_position_pct_of_portfolio": 5.0,
                "mandatory_stop_loss": False,
            }
        },
    )
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=0.6, fundamental=0.8, catalyst=0.8,
        position_pnl_pct=-50.0,  # deep stop-loss breach — must be ignored
    )
    assert rec["sell_pressure"] == 0.0
    assert rec["action"] == "BUY"


def test_stop_loss_and_take_profit_pct_come_from_config(monkeypatch, fixed_config):
    monkeypatch.setattr(
        engine,
        "load_risk_limits",
        lambda: {
            "position": {
                "min_confidence_to_notify": 50,
                "max_position_pct_of_portfolio": 5.0,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 6.0,
            }
        },
    )
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=0.8, fundamental=0.8, catalyst=0.8
    )
    assert rec["stop_loss_pct"] == 2.0
    assert rec["take_profit_pct"] == 6.0


def test_reference_price_recorded_on_the_rec(fixed_config):
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=0.8, fundamental=0.8, catalyst=0.8,
        reference_price=123.45,
    )
    assert rec["reference_price"] == 123.45


def test_reference_price_defaults_to_none(fixed_config):
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.8, technical=0.8, fundamental=0.8, catalyst=0.8
    )
    assert rec["reference_price"] is None


def test_stop_loss_sell_fires_even_below_notify_threshold(fixed_config):
    """Regression: a held position with weak technical/sentiment/catalyst
    (confidence below min_confidence_to_notify) but a deep stop-loss breach
    must still SELL — this is a risk-management cutoff, not a fresh
    conviction call, and shouldn't be silently gated behind the same bar a
    brand-new BUY idea has to clear."""
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.2, technical=0.2, fundamental=0.2, catalyst=0.2,
        position_pnl_pct=-20.0,  # deep past the 4% stop-loss -> sell_pressure 1.0
    )
    assert rec["confidence"] < 50  # would have been HOLD before this fix
    assert rec["action"] == "SELL"


def test_no_sell_pressure_below_threshold_still_holds(fixed_config):
    """Without any position (no sell_pressure), low confidence still HOLDs —
    the fix above only forces SELL when there's real stop-loss/take-profit
    pressure, never as a general override of the notify threshold."""
    rec = engine.score_candidate(
        "TSLA", "midday", sentiment=0.2, technical=0.2, fundamental=0.2, catalyst=0.2,
    )
    assert rec["action"] == "HOLD"

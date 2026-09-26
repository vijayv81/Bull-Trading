import pytest

from trading_agent.scoring import text_signals as ts


# --- sentiment_score -----------------------------------------------------------


def test_sentiment_none_for_empty_text():
    assert ts.sentiment_score("") is None
    assert ts.sentiment_score(None) is None


def test_sentiment_none_when_no_keywords_detected():
    assert ts.sentiment_score("Shares of the company traded in a narrow range today.") is None


def test_sentiment_bullish_text_scores_above_half():
    text = "Analysts upgraded the stock after it beat expectations and rallied to a record high."
    assert ts.sentiment_score(text) > 0.5


def test_sentiment_bearish_text_scores_below_half():
    text = "The company was downgraded after missing estimates; shares plunged amid a lawsuit."
    assert ts.sentiment_score(text) < 0.5


def test_sentiment_mixed_text_scores_near_half():
    text = "The stock rallied on strong demand but was later downgraded after a profit warning."
    score = ts.sentiment_score(text)
    assert 0.3 < score < 0.7


def test_sentiment_word_boundary_avoids_false_positive():
    # "miss" must not match inside "dismissed" — a real false-positive risk
    # for naive substring matching.
    text = "The lawsuit against the company was dismissed by the court."
    # "lawsuit" is bearish, "dismissed" must not also count as a "miss" hit.
    score = ts.sentiment_score(text)
    assert score == 0.0  # only the bearish "lawsuit" hit, no bullish hits


# --- catalyst_score --------------------------------------------------------------


def test_catalyst_none_for_empty_text():
    assert ts.catalyst_score("") is None


def test_catalyst_none_when_nothing_catalyst_shaped():
    assert ts.catalyst_score("Shares traded flat in a quiet session.") is None


def test_catalyst_single_hit_scores_a_third():
    assert ts.catalyst_score("The company reported quarterly earnings today.") == pytest.approx(1 / 3)


def test_catalyst_caps_at_one_past_three_hits():
    text = (
        "The company announced a merger, received FDA approval, raised guidance, "
        "and signed a new partnership contract."
    )
    assert ts.catalyst_score(text) == 1.0

"""The journal is the measurement half of the improvement loop, so the parts
that must hold are: reasoning is stored verbatim, outcomes are only computed
from a real reference price, and marking is re-runnable without double-counting.
"""

import json

import pytest

from trading_agent import journal as j


@pytest.fixture(autouse=True)
def isolated_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(j, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(j, "RECOMMENDATIONS_DIR", tmp_path / "recommendations")
    monkeypatch.setattr(j, "PERFORMANCE_DIR", tmp_path / "performance")


@pytest.fixture
def price(monkeypatch):
    """Control the quote the journal measures from."""

    def _set(value):
        monkeypatch.setattr(j, "_reference_price", lambda ticker: value)

    return _set


def test_record_stores_reasoning_verbatim(price):
    price(250.0)
    words = "gut feel, it held 240 on three retests and I don't trust the guidance raise"
    j.record_entry("tsla", "midday", "approve", words, action="BUY", confidence=72)

    entries = j.load_entries()
    assert len(entries) == 1
    assert entries[0]["reasoning"] == words
    assert entries[0]["ticker"] == "TSLA"
    assert entries[0]["reference_price"] == 250.0
    assert entries[0]["outcome"] is None


def test_record_without_a_quote_still_captures_reasoning(price):
    price(None)
    j.record_entry("TSLA", "midday", "reject", "too extended")

    entry = j.load_entries()[0]
    assert entry["reasoning"] == "too extended"
    assert entry["reference_price"] is None


def test_buy_that_rose_is_directionally_correct(price):
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="BUY")
    price(110.0)

    updated = j.mark_outcomes()
    assert updated[0]["outcome"]["pct_change"] == 10.0
    assert updated[0]["outcome"]["directionally_correct"] is True


def test_buy_that_fell_is_directionally_wrong(price):
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="BUY")
    price(90.0)

    updated = j.mark_outcomes()
    assert updated[0]["outcome"]["pct_change"] == -10.0
    assert updated[0]["outcome"]["directionally_correct"] is False


def test_sell_that_fell_is_directionally_correct(price):
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="SELL")
    price(90.0)

    assert j.mark_outcomes()[0]["outcome"]["directionally_correct"] is True


def test_hold_has_no_direction_to_be_right_about(price):
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="HOLD")
    price(110.0)

    assert j.mark_outcomes()[0]["outcome"]["directionally_correct"] is None


def test_marking_is_rerunnable_without_double_counting(price):
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="BUY")
    price(110.0)

    assert len(j.mark_outcomes()) == 1
    assert j.mark_outcomes() == []  # already measured, left alone

    entry = j.load_entries()[0]
    assert entry["outcome"]["pct_change"] == 10.0  # not remeasured against a newer price


def test_outcome_skipped_when_reference_price_was_never_captured(price):
    price(None)
    j.record_entry("TSLA", "midday", "approve", "why", action="BUY")
    price(110.0)

    assert j.mark_outcomes() == []
    assert j.load_entries()[0]["outcome"] is None


def test_outcome_skipped_when_current_quote_unavailable(price):
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="BUY")
    price(None)

    assert j.mark_outcomes() == []
    assert j.load_entries()[0]["outcome"] is None


def test_entries_persist_across_multiple_records(price):
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "first")
    j.record_entry("GOOG", "midday", "reject", "second")

    assert [e["ticker"] for e in j.load_entries()] == ["TSLA", "GOOG"]


# --- aggregate_performance ---------------------------------------------------


def _write_rec(day, checkpoint, ticker, component_scores):
    path = j.RECOMMENDATIONS_DIR / day
    path.mkdir(parents=True, exist_ok=True)
    rec_file = path / f"recs_{checkpoint}.json"
    recs = json.loads(rec_file.read_text()) if rec_file.exists() else []
    recs.append({"ticker": ticker, "checkpoint": checkpoint, "component_scores": component_scores})
    rec_file.write_text(json.dumps(recs))


def test_aggregate_with_no_journal_history_is_empty():
    metrics = j.aggregate_performance()
    assert metrics["by_ticker"] == {}
    assert metrics["by_signal_type"] == {}


def test_aggregate_writes_strategy_metrics_file(price):
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="BUY")
    price(110.0)
    j.mark_outcomes()

    j.aggregate_performance()
    assert j._strategy_metrics_path().exists()


def test_aggregate_by_ticker_hit_rate(price):
    day = j.today()
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="BUY", day=day)
    price(110.0)
    j.mark_outcomes(day)  # correct
    price(100.0)
    j.record_entry("TSLA", "pre_close", "approve", "why", action="BUY", day=day)
    price(90.0)
    j.mark_outcomes(day)  # wrong

    metrics = j.aggregate_performance()
    assert metrics["by_ticker"]["TSLA"] == {"hit_rate": 0.5, "n": 2}


def test_aggregate_excludes_entries_with_no_direction(price):
    day = j.today()
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="HOLD", day=day)
    price(110.0)
    j.mark_outcomes(day)

    metrics = j.aggregate_performance()
    assert metrics["by_ticker"] == {}


def test_aggregate_by_signal_type_credits_agreeing_leans(price):
    day = j.today()
    _write_rec(day, "midday", "TSLA", {"sentiment": 0.9, "technical": 0.1, "fundamental": None})
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="BUY", day=day)
    price(110.0)  # price rose: sentiment (bullish lean) agrees, technical (bearish lean) doesn't
    j.mark_outcomes(day)

    metrics = j.aggregate_performance()
    assert metrics["by_signal_type"]["sentiment"] == {"hit_rate": 1.0, "n": 1}
    assert metrics["by_signal_type"]["technical"] == {"hit_rate": 0.0, "n": 1}
    assert "fundamental" not in metrics["by_signal_type"]  # None component excluded


def test_aggregate_is_a_full_recompute_not_incremental(price):
    day = j.today()
    price(100.0)
    j.record_entry("TSLA", "midday", "approve", "why", action="BUY", day=day)
    price(110.0)
    j.mark_outcomes(day)
    first = j.aggregate_performance()
    second = j.aggregate_performance()
    assert first["by_ticker"] == second["by_ticker"]

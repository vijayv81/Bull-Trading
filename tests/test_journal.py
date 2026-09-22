"""The journal is the measurement half of the improvement loop, so the parts
that must hold are: reasoning is stored verbatim, outcomes are only computed
from a real reference price, and marking is re-runnable without double-counting.
"""

import pytest

from trading_agent import journal as j


@pytest.fixture(autouse=True)
def isolated_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(j, "JOURNAL_DIR", tmp_path / "journal")


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

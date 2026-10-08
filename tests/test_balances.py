"""Opening balance, closing balance and the net difference at the top of the
daily and weekly summaries."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trading_agent.reporting import balances as bal

ET = ZoneInfo("America/New_York")
# ET session date -> equity at that session's close (get_equity_by_close()'s shape)
HISTORY = {
    "2026-09-17": 100000.0, "2026-09-25": 101021.36, "2026-10-01": 96459.36,
    "2026-10-02": 95458.93, "2026-10-05": 94273.68, "2026-10-06": 95440.16,
}


@pytest.fixture
def alpaca(monkeypatch):
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_equity_by_close", lambda: dict(HISTORY))
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", lambda: {"equity": "95312.60"})


def _at(day, hh=20, mm=47):
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_today_opens_at_the_prior_close_and_closes_at_live_equity(alpaca):
    b = bal.window_balances("2026-10-07", "2026-10-07", now=_at("2026-10-07"))
    assert b["opening"] == 95440.16 and b["opening_note"] == "close of 2026-10-06"
    assert b["closing"] == 95312.60 and b["closing_note"] == "as of 20:47 ET"
    assert b["net_usd"] == -127.56 and b["net_pct"] == -0.13 and b["error"] is None


def test_a_past_day_uses_the_recorded_closes(alpaca):
    b = bal.window_balances("2026-10-06", "2026-10-06", now=_at("2026-10-07"))
    assert (b["opening"], b["closing"]) == (94273.68, 95440.16)
    assert b["closing_note"] == "close of 2026-10-06" and b["net_usd"] == 1166.48 and b["net_pct"] == 1.24


def test_a_week_opens_at_the_close_before_monday(alpaca):
    b = bal.window_balances("2026-10-05", "2026-10-11", now=_at("2026-10-07"))  # window still running
    assert b["opening"] == 95458.93 and b["opening_note"] == "close of 2026-10-02"
    assert b["closing_note"].startswith("as of")


def test_a_finished_week_closes_at_its_last_session_even_across_a_weekend(alpaca):
    b = bal.window_balances("2026-09-28", "2026-10-04", now=_at("2026-10-07"))  # ends on a Sunday
    assert (b["opening"], b["closing"]) == (101021.36, 95458.93)
    assert b["closing_note"] == "close of 2026-10-02" and b["net_usd"] == -5562.43


def test_a_window_older_than_the_account_opens_at_its_first_recorded_balance(alpaca):
    b = bal.window_balances("2026-09-04", "2026-10-07", now=_at("2026-10-07"))
    assert b["opening"] == 100000.0
    assert b["opening_note"] == "the account's first recorded balance, 2026-09-17"


def test_unreachable_history_is_an_error_not_zero(monkeypatch):
    def boom():
        raise RuntimeError("alpaca down")

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_equity_by_close", boom)
    b = bal.window_balances("2026-10-07", "2026-10-07", now=_at("2026-10-07"))
    assert b["error"] == "alpaca down" and b["opening"] is None and b["net_usd"] is None
    assert bal.balance_lines(b) == ["Account balance: unavailable — alpaca down"]


def test_unreachable_live_account_keeps_the_opening_and_marks_the_closing_unavailable(monkeypatch):
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_equity_by_close", lambda: dict(HISTORY))

    def boom():
        raise RuntimeError("account down")

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_account", boom)
    b = bal.window_balances("2026-10-07", "2026-10-07", now=_at("2026-10-07"))
    assert bal.balance_lines(b) == [
        "Opening balance: $95,440.16 (close of 2026-10-06)",
        "Closing balance: unavailable — account down",
    ]


def test_empty_history_is_unavailable(monkeypatch):
    monkeypatch.setattr("trading_agent.data.alpaca_client.get_equity_by_close", lambda: {})
    assert bal.window_balances("2026-10-07", "2026-10-07")["error"] == "no equity history available"


def test_lines_name_the_direction(alpaca):
    down = bal.balance_lines(bal.window_balances("2026-10-07", "2026-10-07", now=_at("2026-10-07")))
    assert down == [
        "Opening balance: $95,440.16 (close of 2026-10-06)",
        "Closing balance: $95,312.60 (as of 20:47 ET)",
        "Net difference: -$127.56 (-0.13%) — net decrease",
    ]
    up = bal.balance_lines(bal.window_balances("2026-10-06", "2026-10-06", now=_at("2026-10-07")))
    assert up[-1] == "Net difference: +$1,166.48 (+1.24%) — net increase"


def test_get_equity_by_close_dates_each_point_by_the_et_session(monkeypatch):
    """Alpaca stamps daily points 00:00 UTC = 8pm ET the evening BEFORE: the
    point stamped 10-07 is Tuesday 10-06's close."""
    from datetime import timezone

    from trading_agent.data import alpaca_client as ac

    def ts(y, m, d):
        return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())

    class History:
        timestamp = [ts(2026, 9, 16), ts(2026, 10, 3), ts(2026, 10, 6), ts(2026, 10, 7)]
        equity = [0.0, 95458.93, 94273.68, 95440.16]

    class Client:
        def get_portfolio_history(self, request):
            return History()

    monkeypatch.setattr(ac, "trading_client", lambda: Client())
    assert ac.get_equity_by_close() == {"2026-10-02": 95458.93, "2026-10-05": 94273.68, "2026-10-06": 95440.16}

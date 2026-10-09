"""Day-aware daily-bar helpers: the daily summary must compare the SAME session's
move, never whichever bar happens to be latest."""

from trading_agent.data import alpaca_client as ac


def _bars(*pairs):
    return lambda symbol, lookback_days=120: [
        {"close": close, "timestamp": f"{day}T04:00:00Z"} for day, close in pairs
    ]


def test_bar_et_date_handles_edt_and_est_midnight_stamps():
    assert ac._bar_et_date({"timestamp": "2026-10-07T04:00:00Z"}) == "2026-10-07"   # EDT: 00:00 ET
    assert ac._bar_et_date({"timestamp": "2026-12-07T05:00:00Z"}) == "2026-12-07"   # EST: 00:00 ET
    assert ac._bar_et_date({"timestamp": "2026-10-07T04:00:00+00:00"}) == "2026-10-07"


def test_return_for_a_day_is_that_sessions_close_vs_the_prior(monkeypatch):
    monkeypatch.setattr(ac, "get_recent_bars", _bars(("2026-10-05", 100.0), ("2026-10-06", 102.0), ("2026-10-07", 99.96)))
    assert ac.get_market_return_pct("SPY", day="2026-10-07") == -2.0
    assert ac.get_market_return_pct("SPY", day="2026-10-06") == 2.0


def test_return_for_a_day_is_unavailable_without_that_days_bar_or_a_prior_bar(monkeypatch):
    monkeypatch.setattr(ac, "get_recent_bars", _bars(("2026-10-05", 100.0), ("2026-10-06", 102.0)))
    assert ac.get_market_return_pct("SPY", day="2026-10-07") is None   # not published yet
    assert ac.get_market_return_pct("SPY", day="2026-10-05") is None   # nothing before it
    assert ac.get_market_return_pct("SPY") == 2.0                      # day-less keeps the old "latest bars" meaning


def test_return_for_a_day_tolerates_bars_without_timestamps(monkeypatch):
    monkeypatch.setattr(ac, "get_recent_bars", lambda symbol, lookback_days=5: [{"close": 1.0}, {"close": 2.0}])
    assert ac.get_market_return_pct("SPY", day="2026-10-07") is None


def test_return_for_a_day_degrades_to_none_when_bars_are_unreachable(monkeypatch):
    def boom(symbol, lookback_days=5):
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(ac, "get_recent_bars", boom)
    assert ac.get_market_return_pct("SPY", day="2026-10-07") is None


def test_closing_price_is_the_close_of_that_sessions_bar(monkeypatch):
    monkeypatch.setattr(ac, "get_recent_bars", _bars(("2026-10-06", 4.0), ("2026-10-07", 4.25)))
    assert ac.get_closing_price("APUS", "2026-10-07") == 4.25
    assert ac.get_closing_price("APUS", "2026-10-08") is None          # no such bar


def test_closing_price_is_none_for_bad_bars_and_failures(monkeypatch):
    monkeypatch.setattr(ac, "get_recent_bars", lambda symbol, lookback_days=120: [{"close": 4.0}])
    assert ac.get_closing_price("APUS", "2026-10-07") is None
    monkeypatch.setattr(ac, "get_recent_bars", _bars(("2026-10-07", 0.0)))
    assert ac.get_closing_price("APUS", "2026-10-07") is None          # a zero close is not a price

    def boom(symbol, lookback_days=120):
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(ac, "get_recent_bars", boom)
    assert ac.get_closing_price("APUS", "2026-10-07") is None

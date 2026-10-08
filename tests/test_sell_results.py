"""The net result of every executed SELL, from Alpaca's own fills (average
cost per symbol), for the daily and weekly summaries."""

from datetime import datetime, timezone

import pytest

from trading_agent.reporting import sell_results as sr


def _fill(oid, symbol, side, qty, price, filled_at):
    return {
        "id": oid, "symbol": symbol, "side": side, "filled_qty": str(qty),
        "filled_avg_price": str(price), "filled_at": filled_at,
    }


# 2026-10-05 14:00Z = 10:00 ET, a normal market-hours fill
T1, T2, T3 = "2026-10-05T14:00:00+00:00", "2026-10-06T14:00:00+00:00", "2026-10-07T14:00:00+00:00"


def test_a_sell_realizes_price_minus_average_cost():
    fills = [
        _fill("b1", "AAA", "OrderSide.BUY", 100, 10.0, T1),
        _fill("b2", "AAA", "OrderSide.BUY", 100, 20.0, T1.replace("14", "15")),
        _fill("s1", "AAA", "OrderSide.SELL", 50, 18.0, T2),
    ]
    [sell] = sr.per_sell_results(fills)
    assert sell["avg_cost"] == 15.0
    assert sell["net_usd"] == 150.0 and sell["net_pct"] == 20.0 and sell["day"] == "2026-10-06"


def test_average_cost_survives_a_partial_sell_and_resets_after_a_full_close():
    fills = [
        _fill("b1", "AAA", "buy", 100, 10.0, T1),
        _fill("s1", "AAA", "sell", 40, 12.0, T1.replace("14", "15")),   # +80, avg stays 10
        _fill("s2", "AAA", "sell", 60, 9.0, T2),                        # -60, closes
        _fill("b2", "AAA", "buy", 10, 50.0, T2.replace("14", "15")),    # fresh basis
        _fill("s3", "AAA", "sell", 10, 55.0, T3),                       # +50 against 50, not 10
    ]
    assert [s["net_usd"] for s in sr.per_sell_results(fills)] == [80.0, -60.0, 50.0]


def test_a_sell_with_no_known_basis_is_listed_but_not_netted():
    fills = [_fill("s1", "ZZZ", "sell", 5, 3.0, T1), _fill("b1", "AAA", "buy", 10, 1.0, T1), _fill("s2", "AAA", "sell", 20, 2.0, T2)]
    results = sr.per_sell_results(fills)
    assert [r["net_usd"] for r in results] == [None, None]  # never held ZZZ; sold more AAA than it had
    s = sr.summarize(fills, "2026-10-05", "2026-10-07")
    assert s["unknown_basis"] == 2 and s["total_net_usd"] == 0.0 and s["by_symbol"] == {}


def test_summary_covers_only_the_window_in_eastern_time():
    fills = [
        _fill("b1", "AAA", "buy", 100, 10.0, T1),
        _fill("s1", "AAA", "sell", 10, 11.0, T2),                         # 10-06 ET
        _fill("s2", "AAA", "sell", 10, 9.0, "2026-10-08T01:30:00+00:00"),  # 21:30 ET on 10-07, not 10-08
    ]
    assert sr.summarize(fills, "2026-10-06", "2026-10-06")["total_net_usd"] == 10.0
    assert sr.summarize(fills, "2026-10-07", "2026-10-07")["total_net_usd"] == -10.0
    assert sr.summarize(fills, "2026-10-08", "2026-10-08")["count"] == 0


def test_summary_totals_percent_symbols_source_and_pending():
    fills = [
        _fill("b1", "AAA", "buy", 100, 10.0, T1),
        _fill("b2", "BBB", "buy", 100, 5.0, T1),
        _fill("s1", "AAA", "sell", 50, 12.0, T2),   # +100 on 500 cost
        _fill("s2", "BBB", "sell", 100, 4.0, T2),   # -100 on 500 cost
        _fill("s3", "AAA", "sell", 10, 9.0, T3),    # -10, later day
    ]
    trades = [
        {"order": {"id": "s1", "side": "sell"}, "source": "auto"},
        {"order": {"id": "s2", "side": "sell"}, "source": "human"},
        {"order": {"id": "never-filled", "side": "sell"}, "source": "auto"},
        {"order": {"id": "buy-pending", "side": "buy"}, "source": "auto"},
    ]
    s = sr.summarize(fills, "2026-10-06", "2026-10-06", trades)
    assert s["count"] == 2 and s["total_net_usd"] == 0.0 and s["total_net_pct"] == 0.0
    assert s["by_symbol"] == {"AAA": 100.0, "BBB": -100.0}
    assert {x["ticker"]: x["source"] for x in s["sells"]} == {"AAA": "auto", "BBB": "human"}
    assert s["pending"] == 1  # only the unfilled SELL; pending BUYs don't matter here


def test_unreachable_alpaca_is_an_error_never_zero(monkeypatch):
    def boom():
        raise RuntimeError("alpaca down")

    monkeypatch.setattr("trading_agent.data.alpaca_client.get_filled_orders", boom)
    result = sr.window_sell_results("2026-10-05", "2026-10-05")
    assert result["error"] == "alpaca down" and result["total_net_usd"] is None
    assert sr.sell_result_lines(result, "today") == ["Net result on sell orders: unavailable — alpaca down"]


def test_lines_say_increase_decrease_or_no_change():
    fills = [_fill("b1", "AAA", "buy", 100, 10.0, T1), _fill("s1", "AAA", "sell", 10, 12.0, T2),
             _fill("b2", "BBB", "buy", 100, 10.0, T1), _fill("s2", "BBB", "sell", 10, 7.0, T3)]
    up = sr.sell_result_lines(sr.summarize(fills, "2026-10-06", "2026-10-06"), "today")
    assert up[0] == "Net result on 1 sell order(s) executed today: +$20.00 (+20.00% on cost) — net increase"
    assert up[1] == "- AAA: sold 10 @ $12.00 vs avg cost $10.00 → +$20.00 (+20.0%)"
    down = sr.sell_result_lines(sr.summarize(fills, "2026-10-07", "2026-10-07"), "today")
    assert "-$30.00" in down[0] and down[0].endswith("net decrease")
    both = sr.sell_result_lines(sr.summarize(fills, "2026-10-06", "2026-10-07"), "this week")
    assert "-$10.00" in both[0] and "net decrease" in both[0] and "this week" in both[0]


def test_lines_for_no_sells_and_for_caveats():
    assert sr.sell_result_lines(sr.summarize([], "2026-10-05", "2026-10-05"), "today") == [
        "No sell orders executed today."
    ]
    lines = sr.sell_result_lines(
        sr.summarize([_fill("s1", "ZZZ", "sell", 5, 3.0, T1)], "2026-10-05", "2026-10-05",
                     [{"order": {"id": "x", "side": "sell"}}]),
        "today",
    )
    assert any("cost basis unavailable, not counted" in l for l in lines)
    assert any("without a known cost basis" in l for l in lines)
    assert any("not filled yet" in l for l in lines)


def test_sub_dollar_prices_keep_four_decimals():
    fills = [_fill("b1", "PENNY", "buy", 1000, 0.0084, T1), _fill("s1", "PENNY", "sell", 1000, 0.011, T2)]
    [line] = [l for l in sr.sell_result_lines(sr.summarize(fills, "2026-10-06", "2026-10-06"), "today") if l.startswith("- ")]
    assert "$0.0110" in line and "$0.0084" in line


# --- get_filled_orders ---------------------------------------------------------


class _FakeOrder:
    def __init__(self, oid, filled_qty, filled_at, submitted_at=datetime(2026, 10, 1, tzinfo=timezone.utc)):
        self.id, self.filled_qty, self.filled_at, self.submitted_at = oid, filled_qty, filled_at, submitted_at

    def model_dump(self, mode="json"):
        return {"id": self.id, "filled_qty": self.filled_qty, "filled_at": self.filled_at}


def test_get_filled_orders_keeps_filled_sorts_by_fill_time_and_pages(monkeypatch):
    from trading_agent.data import alpaca_client as ac

    page1 = [_FakeOrder(f"o{i}", "0" if i % 2 else "5", f"2026-10-05T14:{i % 60:02d}:00+00:00") for i in range(500)]
    page2 = [_FakeOrder("late", "3", "2026-10-05T13:00:00+00:00"), _FakeOrder("dup", "1", "2026-10-05T12:00:00+00:00")]
    page2.append(page1[0])  # the same order again must not repeat
    calls = []

    class Client:
        def get_orders(self, request):
            calls.append(request.after)
            return page1 if len(calls) == 1 else page2

    monkeypatch.setattr(ac, "trading_client", lambda: Client())
    out = ac.get_filled_orders()

    assert len(calls) == 2 and calls[1] == page1[-1].submitted_at  # paged on from the last order
    ids = [o["id"] for o in out]
    assert ids.count("o0") == 1 and "o1" not in ids  # unfilled (qty 0) dropped, duplicate dropped
    assert ids[0] == "dup" and ids[1] == "late"  # oldest fill first
    assert [o["filled_at"] for o in out] == sorted(o["filled_at"] for o in out)

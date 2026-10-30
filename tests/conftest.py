"""Test-wide safety net: no test may send real email/SMS, place an Alpaca order,
or write into the repository's real data/ directories.

Added 2026-10-21 after two test runs, before a new code path (liquid candidate
sourcing) was stubbed, reached live research and live auto-apply: they wrote
recommendations, approvals and attempt records into data/ for that day and very
likely emailed four "research outage" alerts made of test stub errors. The order
path refused only because the test's trimmed risk config lacked a key. This makes
those outcomes impossible regardless of what a test forgets to stub.

Tests that check what was sent, or where a file went, still monkeypatch these
same names themselves; their patches apply on top of these.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_external_side_effects(monkeypatch, tmp_path, request):
    from trading_agent.data import alpaca_client, market_data
    from trading_agent.execute import auto_pilot, order_manager
    from trading_agent.notify import approval_gateway, senders
    from trading_agent.reporting import report_builder
    from trading_agent.scoring import recommendation_engine
    from trading_agent import guardrails, journal, optimizations

    sent = []
    if request.module.__name__.split(".")[-1] != "test_senders":
        # test_senders exercises the real senders against its own fake HTTP layer.
        monkeypatch.setattr(senders, "send_email", lambda *a, **k: sent.append(("email", a)))
        monkeypatch.setattr(senders, "send_sms", lambda *a, **k: sent.append(("sms", a)))

    def no_orders(*a, **k):
        raise RuntimeError("tests must not place Alpaca orders")

    for module in (alpaca_client, order_manager):
        monkeypatch.setattr(module, "submit_market_order", no_orders)
        monkeypatch.setattr(module, "submit_limit_order", no_orders)

    # No live market data either: the trade and delayed-quote lookups fail, so code
    # falls back to the (test-stubbed) IEX quote. Tests that need them patch them.
    def no_live_data(*a, **k):
        raise RuntimeError("tests must not read live market data")

    monkeypatch.setattr(alpaca_client, "get_latest_trade", no_live_data)
    monkeypatch.setattr(alpaca_client, "get_delayed_quote", no_live_data)
    # No live fill history: the re-entry cooldown reads it; empty means "no recent exits".
    # test_sell_results exercises get_filled_orders itself against a fake client.
    if request.module.__name__.split(".")[-1] != "test_sell_results":
        monkeypatch.setattr(alpaca_client, "get_filled_orders", lambda since=None: [])

    data = tmp_path / "_data"
    for module, names in (
        (approval_gateway, ("APPROVALS_DIR", "RECOMMENDATIONS_DIR")),
        (auto_pilot, ("TRADES_DIR",)),
        (order_manager, ("TRADES_DIR",)),
        (guardrails, ("TRADES_DIR",)),
        (report_builder, ("APPROVALS_DIR", "PERFORMANCE_DIR", "RECOMMENDATIONS_DIR", "REPORTS_DIR", "TRADES_DIR")),
        (recommendation_engine, ("PERFORMANCE_DIR",)),
        (journal, ("JOURNAL_DIR", "PERFORMANCE_DIR", "RECOMMENDATIONS_DIR")),
        (optimizations, ("OPTIMIZATIONS_DIR",)),
    ):
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, data / f"{module.__name__}.{name}")
    monkeypatch.setattr(market_data, "REFERENCE_CACHE_PATH", data / "yahoo_reference.json")
    return sent

"""One ticker's research/scoring failure must never crash the whole
checkpoint — this is exactly what happened in production: a Perplexity
ReadTimeout on one mover ticker killed the run before the real watchlist was
ever researched.
"""

import pytest

from trading_agent import orchestrator as orch


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(orch, "daily_loss_reason", lambda: None)
    monkeypatch.setattr(orch, "load_watchlist", lambda: ["GOOD", "BAD"])
    monkeypatch.setattr(orch, "get_market_movers", lambda: {"gainers": []})
    monkeypatch.setattr(orch, "get_recent_bars", lambda ticker: [])
    monkeypatch.setattr(orch, "technical_score", lambda bars: 0.5)
    monkeypatch.setattr(orch, "save_recommendation", lambda rec: None)
    monkeypatch.setattr(orch, "auto_apply", lambda recs, checkpoint: [])
    monkeypatch.setattr(
        orch,
        "score_candidate",
        lambda **kwargs: {
            "ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"],
            "action": "HOLD", "confidence": 50,
        },
    )


def _capture_digest(monkeypatch):
    calls = []
    monkeypatch.setattr(
        orch,
        "notify_digest",
        lambda recs, checkpoint, auto_results=None, failed_tickers=None: calls.append(
            {"recs": recs, "auto_results": auto_results, "failed_tickers": failed_tickers}
        ),
    )
    return calls


def test_one_ticker_failure_does_not_stop_the_checkpoint(monkeypatch):
    def research(ticker, checkpoint):
        if ticker == "BAD":
            raise TimeoutError("Perplexity timed out")
        return {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": ["http://x"]}

    monkeypatch.setattr(orch, "research_ticker", research)
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert [r["ticker"] for r in results] == ["GOOD"]
    assert digest_calls[0]["failed_tickers"] == [{"ticker": "BAD", "error": "Perplexity timed out"}]


def test_failure_in_bars_or_scoring_is_also_isolated(monkeypatch):
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )

    def flaky_bars(ticker):
        if ticker == "BAD":
            raise RuntimeError("alpaca data unavailable")
        return []

    monkeypatch.setattr(orch, "get_recent_bars", flaky_bars)
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert [r["ticker"] for r in results] == ["GOOD"]
    assert digest_calls[0]["failed_tickers"][0]["ticker"] == "BAD"


def test_all_tickers_succeeding_reports_no_failures(monkeypatch):
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert {r["ticker"] for r in results} == {"GOOD", "BAD"}
    assert digest_calls[0]["failed_tickers"] == []


def test_all_tickers_failing_still_completes_with_empty_results(monkeypatch):
    def research(ticker, checkpoint):
        raise TimeoutError("Perplexity timed out")

    monkeypatch.setattr(orch, "research_ticker", research)
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert results == []
    assert len(digest_calls[0]["failed_tickers"]) == 2

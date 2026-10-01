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
    monkeypatch.setattr(orch, "get_positions", lambda: [])
    monkeypatch.setattr(orch, "get_recent_bars", lambda ticker, **kwargs: [])
    monkeypatch.setattr(orch, "get_market_return_pct", lambda symbol: None)
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
        lambda recs, checkpoint, auto_results=None, failed_tickers=None, data_quality_alert=None: calls.append(
            {
                "recs": recs,
                "auto_results": auto_results,
                "failed_tickers": failed_tickers,
                "data_quality_alert": data_quality_alert,
            }
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

    def flaky_bars(ticker, **kwargs):
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


def test_movers_losers_are_researched_alongside_gainers(monkeypatch):
    """get_market_movers() has always fetched both gainers and losers, but
    only gainers were ever added to the research universe — 100% biased
    toward names already up, and the same narrow slice of tickers
    dominating day after day. Both must now seed candidates."""
    monkeypatch.setattr(
        orch, "get_market_movers",
        lambda: {
            "gainers": [{"symbol": "GAINER", "price": 50.0}],
            "losers": [{"symbol": "LOSER", "price": 50.0}],
        },
    )
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert {r["ticker"] for r in results} == {"GOOD", "BAD", "GAINER", "LOSER"}
    assert digest_calls[0]["failed_tickers"] == []


# --- candidate_min_price: a liquidity floor on NEW candidate sourcing ---------


def test_candidate_min_price_excludes_penny_movers(monkeypatch):
    """Per user instruction 2026-10-01: a mover priced below the floor never
    enters research/scoring — these are simultaneously the biggest losers
    and the ones most likely to trip stale_recommendation_reason()."""
    monkeypatch.setattr(orch, "load_risk_limits", lambda: {"position": {"candidate_min_price": 1.0}})
    monkeypatch.setattr(
        orch, "get_market_movers",
        lambda: {
            "gainers": [{"symbol": "PENNY", "price": 0.5}, {"symbol": "REAL", "price": 50.0}],
            "losers": [{"symbol": "WARRANT", "price": 0.02}],
        },
    )
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    tickers = {r["ticker"] for r in results}
    assert "REAL" in tickers
    assert "PENNY" not in tickers
    assert "WARRANT" not in tickers


def test_candidate_min_price_unset_includes_everything(monkeypatch):
    monkeypatch.setattr(orch, "load_risk_limits", lambda: {"position": {}})
    monkeypatch.setattr(
        orch, "get_market_movers",
        lambda: {"gainers": [{"symbol": "PENNY", "price": 0.001}], "losers": []},
    )
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert "PENNY" in {r["ticker"] for r in results}


def test_candidate_min_price_excludes_a_mover_with_no_price_field(monkeypatch):
    monkeypatch.setattr(orch, "load_risk_limits", lambda: {"position": {"candidate_min_price": 1.0}})
    monkeypatch.setattr(
        orch, "get_market_movers",
        lambda: {"gainers": [{"symbol": "NOPRICE"}], "losers": []},
    )
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert "NOPRICE" not in {r["ticker"] for r in results}


def test_candidate_min_price_does_not_apply_to_held_positions(monkeypatch):
    """Continuous position monitoring must keep scoring a held position
    regardless of its price — otherwise a stop-loss SELL could never even
    be proposed for a penny stock already held."""
    monkeypatch.setattr(orch, "load_risk_limits", lambda: {"position": {"candidate_min_price": 1.0}})
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "HELDPENNY", "unrealized_plpc": "-0.3"}])
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert "HELDPENNY" in {r["ticker"] for r in results}


def test_all_tickers_failing_still_completes_with_empty_results(monkeypatch):
    def research(ticker, checkpoint):
        raise TimeoutError("Perplexity timed out")

    monkeypatch.setattr(orch, "research_ticker", research)
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert results == []
    assert len(digest_calls[0]["failed_tickers"]) == 2


# --- insufficient bar history must not masquerade as a real technical signal --


def test_technical_is_none_when_bars_insufficient(monkeypatch):
    """Regression test: get_recent_bars(lookback_days=60) used to return fewer
    trading days than technical_score()'s 50-day SMA window needs, so every
    call silently fell back to the neutral 0.5 — for every ticker, every run.
    orchestrator must now pass technical=None to score_candidate() instead,
    so that fallback can never again look like a real per-ticker signal.
    """
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    monkeypatch.setattr(
        orch, "get_recent_bars", lambda ticker, **kwargs: [{"close": 1.0}] * 10
    )  # < MIN_BARS_FOR_TECHNICAL
    monkeypatch.setattr(orch, "technical_score", lambda bars: pytest.fail("must not be called"))
    seen_technical = []
    monkeypatch.setattr(
        orch,
        "score_candidate",
        lambda **kwargs: (
            seen_technical.append(kwargs["technical"])
            or {"ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"], "action": "HOLD", "confidence": 50}
        ),
    )
    _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    assert seen_technical == [None, None]


# --- flat/identical confidence across candidates is a data-quality failure ----


def test_flat_confidence_across_candidates_skips_auto_apply_and_alerts(monkeypatch):
    monkeypatch.setattr(orch, "load_watchlist", lambda: ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 1.0, "headline_summary": "ok", "sources": ["u"]},
    )

    def fake_score(**kwargs):
        return {
            "ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"],
            "action": "BUY", "confidence": 72.0,
        }

    monkeypatch.setattr(orch, "score_candidate", fake_score)

    auto_apply_calls = []
    monkeypatch.setattr(orch, "auto_apply", lambda recs, checkpoint: auto_apply_calls.append(recs) or [])
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert len(results) == 3
    assert auto_apply_calls == []  # never even called — a flat score must not reach auto_apply
    assert digest_calls[0]["data_quality_alert"] is not None
    assert "identical confidence" in digest_calls[0]["data_quality_alert"]


def test_varied_confidence_does_not_trigger_the_alert(monkeypatch):
    monkeypatch.setattr(orch, "load_watchlist", lambda: ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 1.0, "headline_summary": "ok", "sources": ["u"]},
    )

    confidences = {"AAA": 60.0, "BBB": 75.0, "CCC": 90.0}

    def fake_score(**kwargs):
        return {
            "ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"],
            "action": "BUY", "confidence": confidences[kwargs["ticker"]],
        }

    monkeypatch.setattr(orch, "score_candidate", fake_score)
    monkeypatch.setattr(orch, "auto_apply", lambda recs, checkpoint: [])
    digest_calls = _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    assert digest_calls[0]["data_quality_alert"] is None


def test_two_identical_candidates_does_not_trigger_the_alert(monkeypatch):
    """Below FLAT_CONFIDENCE_MIN_CANDIDATES (3) two matching scores are as
    likely to be coincidence as a real failure — the fixture's default
    watchlist (GOOD, BAD) exercises exactly that boundary."""
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": ["u"]},
    )
    monkeypatch.setattr(
        orch, "score_candidate",
        lambda **kwargs: {
            "ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"], "action": "BUY", "confidence": 72.0,
        },
    )
    digest_calls = _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    assert digest_calls[0]["data_quality_alert"] is None


# --- continuous monitoring of currently-held positions --------------------------


def test_held_position_not_on_watchlist_is_still_researched(monkeypatch):
    """A ticker bought today but never added to the watchlist, and no longer
    a 'mover', must still get researched/scored every checkpoint — otherwise
    no future checkpoint would ever propose a SELL for it again."""
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "HELD", "unrealized_plpc": "0.02"}])
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert {r["ticker"] for r in results} == {"GOOD", "BAD", "HELD"}


def test_held_position_pnl_pct_passed_to_score_candidate(monkeypatch):
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "HELD", "unrealized_plpc": "-0.045"}])
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    seen = {}

    def fake_score(**kwargs):
        seen[kwargs["ticker"]] = kwargs["position_pnl_pct"]
        return {"ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"], "action": "HOLD", "confidence": 50}

    monkeypatch.setattr(orch, "score_candidate", fake_score)
    _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    assert seen["HELD"] == pytest.approx(-4.5)
    assert seen["GOOD"] is None  # not held — no position P&L to pass
    assert seen["BAD"] is None


def test_unreadable_positions_does_not_block_the_checkpoint(monkeypatch):
    def boom():
        raise RuntimeError("alpaca unreachable")

    monkeypatch.setattr(orch, "get_positions", boom)
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    digest_calls = _capture_digest(monkeypatch)

    results = orch.run_checkpoint("pre_open")

    assert {r["ticker"] for r in results} == {"GOOD", "BAD"}  # watchlist alone, degrades gracefully


def test_held_option_symbol_is_filtered_out(monkeypatch):
    monkeypatch.setattr(
        orch, "get_positions", lambda: [{"symbol": "AAPL240119C00150000", "unrealized_plpc": "0.1"}]
    )
    researched = []

    def research(ticker, checkpoint):
        researched.append(ticker)
        return {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []}

    monkeypatch.setattr(orch, "research_ticker", research)
    _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    assert "AAPL240119C00150000" not in researched
    assert set(researched) == {"GOOD", "BAD"}


# --- real sentiment/catalyst text signals (not the old source-presence proxy) --


def test_sentiment_and_catalyst_derived_from_research_text(monkeypatch):
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {
            "confidence_of_extraction": 1.0,
            "headline_summary": "Analysts upgraded the stock after strong earnings beat expectations.",
            "sources": ["http://x"],
        },
    )
    seen = {}

    def fake_score(**kwargs):
        seen[kwargs["ticker"]] = (kwargs["sentiment"], kwargs["catalyst"])
        return {"ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"], "action": "HOLD", "confidence": 50}

    monkeypatch.setattr(orch, "score_candidate", fake_score)
    _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    sentiment, catalyst = seen["GOOD"]
    assert sentiment is not None and sentiment > 0.5  # bullish keywords present
    assert catalyst is not None and catalyst > 0.0  # "earnings", "upgraded" present


def test_sentiment_and_catalyst_none_when_research_text_has_no_signal(monkeypatch):
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {
            "confidence_of_extraction": 1.0,
            "headline_summary": "Shares traded flat in a quiet session.",
            "sources": ["http://x"],
        },
    )
    seen = {}

    def fake_score(**kwargs):
        seen[kwargs["ticker"]] = (kwargs["sentiment"], kwargs["catalyst"])
        return {"ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"], "action": "HOLD", "confidence": 50}

    monkeypatch.setattr(orch, "score_candidate", fake_score)
    _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    sentiment, catalyst = seen["GOOD"]
    assert sentiment is None
    assert catalyst is None


# --- held positions get their own, earlier pass (react faster on exits) --------


def test_held_positions_are_researched_and_auto_applied_before_watchlist(monkeypatch):
    """2026-09-29: every stop-loss-triggered SELL that day was refused,
    almost certainly on staleness — held positions sat researched-but-
    unexecuted for minutes while the rest of a long watchlist/movers batch
    was still being researched. Held positions must now be scored, and have
    auto_apply() called on them, in their own pass BEFORE the watchlist/
    movers batch is even researched, so their price/technical re-check at
    execution time is as fresh as possible.
    """
    monkeypatch.setattr(orch, "get_positions", lambda: [{"symbol": "HELD", "unrealized_plpc": "-0.1"}])
    research_order = []

    def research(ticker, checkpoint):
        research_order.append(ticker)
        return {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []}

    monkeypatch.setattr(orch, "research_ticker", research)

    auto_apply_batches = []
    monkeypatch.setattr(
        orch, "auto_apply",
        lambda recs, checkpoint: auto_apply_batches.append([r["ticker"] for r in recs]) or [],
    )
    _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    assert research_order[0] == "HELD"
    assert set(research_order[1:]) == {"GOOD", "BAD"}
    # auto_apply is called once for the held-position batch, then once more
    # for the watchlist batch — not a single combined call at the end.
    assert auto_apply_batches == [["HELD"], ["BAD", "GOOD"]]


def test_auto_apply_not_called_for_an_empty_batch(monkeypatch):
    # No held positions at all (the fixture default: get_positions -> []) —
    # auto_apply must be called exactly once, for the watchlist batch, never
    # a spurious extra call for an empty held-positions batch.
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    auto_apply_batches = []
    monkeypatch.setattr(
        orch, "auto_apply",
        lambda recs, checkpoint: auto_apply_batches.append([r["ticker"] for r in recs]) or [],
    )
    _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    assert len(auto_apply_batches) == 1
    assert set(auto_apply_batches[0]) == {"GOOD", "BAD"}


# --- market_return_pct: read once per checkpoint, forwarded to every ticker ----


def test_market_return_pct_fetched_once_and_forwarded_to_every_ticker(monkeypatch):
    calls = {"count": 0}

    def fake_market_return(symbol):
        calls["count"] += 1
        assert symbol == "SPY"
        return -1.5

    monkeypatch.setattr(orch, "get_market_return_pct", fake_market_return)
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    seen = {}

    def fake_score(**kwargs):
        seen[kwargs["ticker"]] = kwargs["market_return_pct"]
        return {"ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"], "action": "HOLD", "confidence": 50}

    monkeypatch.setattr(orch, "score_candidate", fake_score)
    _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    assert calls["count"] == 1  # not once per ticker
    assert seen == {"GOOD": -1.5, "BAD": -1.5}


def test_long_term_trend_computed_and_forwarded_to_score_candidate(monkeypatch):
    # Bars are short (the fixture default: []), so the real long_term_trend()
    # correctly reports None — but it must actually be called and forwarded,
    # not silently dropped from the score_candidate() call.
    monkeypatch.setattr(
        orch, "research_ticker",
        lambda ticker, checkpoint: {"confidence_of_extraction": 0.8, "headline_summary": "ok", "sources": []},
    )
    seen = {}

    def fake_score(**kwargs):
        seen[kwargs["ticker"]] = kwargs["long_term_trend_ctx"]
        return {"ticker": kwargs["ticker"], "checkpoint": kwargs["checkpoint"], "action": "HOLD", "confidence": 50}

    monkeypatch.setattr(orch, "score_candidate", fake_score)
    _capture_digest(monkeypatch)

    orch.run_checkpoint("pre_open")

    assert seen == {"GOOD": None, "BAD": None}

"""Lightweight lexicon-based text signals for sentiment/catalyst scoring.

Replaces what `sentiment`/`catalyst` used to be fed from: purely "did the
research call return any source URLs at all" (`confidence_of_extraction`,
and a bare `0.7 if sources else 0.3`) — neither ever looked at what the
research actually said. No external NLP dependency or API: this project's
credential policy (CLAUDE.md §7.1) treats adding a new vendor as a
deliberate, reviewed decision, not something to wire in silently as a
scoring-formula fix. A keyword lexicon over the research text itself
(`headline_summary`, from either Perplexity or the Yahoo Finance fallback —
same field either way) is a real, if crude, read of the text's content.

Both return None (not a fake neutral 0.5/0.3) when the text has no
detectable keywords at all — same "exclude, don't fake" convention
`scoring/recommendation_engine.py` already uses for `fundamental`,
`historical_hitrate`, and `technical` when there isn't enough bar history.
"""

from __future__ import annotations

import re

_BULLISH_TERMS = (
    "upgrade", "upgraded", "beat", "beats", "beating", "surge", "surged", "surging",
    "rally", "rallied", "rallying", "outperform", "buy rating", "raised guidance",
    "raises guidance", "record high", "record profit", "record revenue",
    "strong demand", "bullish", "soar", "soared", "soaring", "breakout",
    "accelerating growth", "price target raised", "beat expectations",
    "better than expected", "strong earnings", "expansion",
)
_BEARISH_TERMS = (
    "downgrade", "downgraded", "miss", "misses", "missed", "plunge", "plunged",
    "plunging", "selloff", "sell rating", "lowered guidance", "lowers guidance",
    "cuts guidance", "record low", "bearish", "recall", "lawsuit", "investigation",
    "layoffs", "restructuring", "profit warning", "guidance cut", "price target cut",
    "slump", "slumped", "slumping", "worse than expected", "weak demand", "warns",
)
_CATALYST_TERMS = (
    "earnings", "earnings report", "earnings call", "fda", "approval", "merger",
    "acquisition", "acquire", "acquires", "buyback", "guidance", "contract",
    "partnership", "spinoff", "spin-off", "ipo", "recall", "lawsuit", "upgrade",
    "downgrade", "analyst", "price target", "product launch", "clinical trial",
    "investigation", "restructuring", "layoffs", "ceo", "resignation",
)


def _count_hits(text: str, terms: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(len(re.findall(rf"\b{re.escape(term)}\b", lowered)) for term in terms)


def sentiment_score(text: str) -> float | None:
    """0 (bearish) to 1 (bullish) from the balance of bullish/bearish keyword
    hits in the research text. None when neither side has any hits at all —
    no sentiment-bearing language detected, excluded from the confidence
    formula rather than defaulting to a fake neutral 0.5.
    """
    if not text:
        return None
    bullish = _count_hits(text, _BULLISH_TERMS)
    bearish = _count_hits(text, _BEARISH_TERMS)
    total = bullish + bearish
    if total == 0:
        return None
    return bullish / total


def catalyst_score(text: str) -> float | None:
    """0-1 from how many distinct catalyst-indicating keywords the research
    text mentions (earnings, M&A, regulatory, guidance, analyst actions,
    ...) — capped at 1.0 by 3 distinct hits, since more than that doesn't
    make a catalyst "more real." None when nothing catalyst-shaped is
    mentioned at all, excluded rather than faked as a flat 0.3.
    """
    if not text:
        return None
    lowered = text.lower()
    hits = sum(1 for term in _CATALYST_TERMS if re.search(rf"\b{re.escape(term)}\b", lowered))
    if hits == 0:
        return None
    return min(1.0, hits / 3)

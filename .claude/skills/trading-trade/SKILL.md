---
name: trading-trade
description: Walks a recommendation through human approval and — only then — paper order submission, respecting every guardrail. Use this whenever the user wants to review pending recommendations, approve or reject one, place or size a trade, or asks "what's waiting on me?" / "buy 10 TSLA" / "execute that". Also use it when an order is refused and they want to know why. This is the only sanctioned path to an Alpaca order; never assemble the CLI calls ad hoc without reading this first.
---

# Approval and paper execution

This skill's entire purpose is to keep a human between a recommendation and an
order. Everything below follows from that.

## Before you start

Read `FEEDBACK.md` in this skill's directory — it carries the user's standing
preferences on sizing, which tickers they want extra scrutiny on, and how they
like decisions presented. Those came from real runs and outrank the generic
guidance here.

## Sync email-link decisions first

Recommendation emails carry Approve/Reject links to a hosted confirmation page
(the "Approval Ticket" artifact). Clicking one and confirming there writes the
decision into that page's own database — not into `data/approvals/` — so it
needs pulling in before you treat `data/approvals/` as complete.

Every time this skill runs, before step 1 below:

1. Read `config/agent_config.yaml -> notifications.approval_ticket_artifact_url`.
   If it's null or missing, skip this section entirely — nothing to sync.
2. `ArtifactData` (`action: "list"`, `collection: "decisions"`, that `url`) to
   read every decision recorded there. Each document carries `ticker`,
   `checkpoint`, `decision`, and `qty` (approve only).
3. For each one, run `trading-agent approvals list <checkpoint>` for its
   checkpoint. If the ticker still shows up as pending, it hasn't been synced
   yet — record it now:
   ```bash
   trading-agent approvals approve <TICKER> <checkpoint> --qty <qty>
   trading-agent approvals reject  <TICKER> <checkpoint>
   ```
   If the ticker no longer shows as pending, a decision already exists for it
   (synced earlier, or made some other way) — leave it alone, never re-record.
4. Mention what you synced, if anything, in one line before showing the
   regular pending list. Silence is fine when there's nothing new.

This is the only place a click on that page becomes an actual approval record —
skip it and clicked decisions just sit in the artifact's database indefinitely.

## The one rule

**You never decide.** Not when the confidence is 95. Not when the user said
last week they liked the setup. Not when they say "do whatever you think." If
asked to approve on their behalf, say plainly that an approval record is the
only thing standing between a recommendation and real (paper) money, and that
it needs to carry their judgment, not yours. Then show them what they need to
decide on.

This isn't a formality to route around — `data/approvals/` is an audit trail
whose value depends entirely on each record reflecting an actual human choice.

## The flow

**1. Show what's pending.**

```bash
trading-agent approvals list <checkpoint>
```

Present ticker, action, confidence, and rationale. If a rationale is thin, say
so — the user deciding on weak evidence should know the evidence is weak.

**2. Record their decision, with their qty.**

```bash
trading-agent approvals approve <TICKER> <checkpoint> --qty <n>
trading-agent approvals reject  <TICKER> <checkpoint>
```

The qty must be the number they gave you. If they didn't give one, ask — do not
derive it from `suggested_size_pct_of_portfolio`, because that field is a
suggestion the scoring engine produced, not a decision anyone made. An approval
recorded with a qty the user never said is exactly the kind of record that looks
like consent without being consent.

**3. Submit, if they ask you to.**

```bash
trading-agent execute <TICKER> <checkpoint> <qty>
```

Approval and execution are deliberately separate steps. Recording an approval is
not permission to immediately submit — confirm they want it sent now.

## When it refuses

`Refused: ...` is the normal, expected outcome — the gate is doing its job, not
erroring. Read the reason back to the user in plain language and stop there:

- **Kill switch off** (`trading_enabled=false`) — the default. Nothing submits
  until they flip it themselves in `config/risk_limits.yaml`. Tell them where it
  is; let them decide.
- **No approval record / not approve / expired** — re-run the approval step.
  An expired approval needs a *fresh decision*, not a re-recorded old one; the
  expiry exists because a 3-hour-old read of a moving market is stale.
- **Qty mismatch beyond 1%** — the approved and requested quantities disagree.
  Resolve it by asking which they meant, never by re-approving at the qty that
  makes the error go away.
- **Over the 5% per-position cap** — includes what they already hold in that
  symbol. Offer the qty that fits, and let them choose it.
- **Daily loss cap reached** — done for the day. Not negotiable, not worth
  re-running.
- **Options contract** — never tradeable here, at any size, under any config.
  There is no flag for this by design.

Never work around a refusal. Don't edit config to unblock an order mid-run,
don't call `alpaca_client.submit_market_order()` directly to skip the gate, and
don't retry with a tweaked qty hoping it slips through. If a guardrail is
genuinely wrong for their strategy, that's a considered code or config change
they make deliberately between runs — not something to patch around while an
order is pending.

## After a fill

Hand off to `trading-journal` to record *why* they made the call while the
reasoning is still fresh. A filled order with no recorded rationale is a data
point the improvement loop can't learn anything from.

## Capturing feedback

When the user corrects you here — "always show me my existing position first",
"don't ask about qty for anything under 5 shares", "flag it when confidence
dropped since the last checkpoint" — append it to `FEEDBACK.md` before the run
ends, in the format that file describes. Confirm what you recorded in one line.

Corrections that touch the safety rules above are the one exception: if the user
wants the approval gate itself loosened, don't quietly record it as a
preference. Say that it's a change to the project's core safety model and should
be a deliberate edit to `CLAUDE.md` and the code, reviewed and committed — not a
line in a feedback file that silently changes how future unattended runs behave.

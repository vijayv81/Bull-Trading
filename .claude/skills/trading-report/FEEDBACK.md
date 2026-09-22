# Feedback: trading-report

Corrections given during real runs. `SKILL.md` reads this file first, and
entries here outrank its generic guidance when the two disagree — they came from
an actual run, it didn't.

This file is tracked in git, so refinements are reviewable and revertable. If an
entry turns out to be wrong, delete it in a commit rather than leaving it to
quietly shape future unattended runs.

## Format

Append newest last:

```
### YYYY-MM-DD — short label
What to do differently, in the imperative.
**Why:** the reason the user gave.
**Scope:** when this applies (which situations, which tickers, always).
```

Record the *why* alongside the rule. Without it, a future run can't tell whether
an edge case the user never mentioned is covered by the entry or not, and will
either over-apply it or ignore it.

Keep entries about how to *work*. A one-off instruction ("skip NVDA today")
belongs in the conversation, not here.

---

_No feedback recorded yet._

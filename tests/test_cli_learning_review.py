"""The two-phase `weekly-learning-review` flow the Saturday routine uses:
--no-send writes the report + a snapshot, --send-saved emails exactly that
snapshot with the PR link (so the emailed numbers are the committed ones)."""

import argparse
import json

import pytest

from trading_agent import cli


def _review():
    return {
        "window_start": "2026-09-04", "window_end": "2026-10-03", "lookback_days": 30,
        "trades_count": 1, "executed_count": 1, "actionable_count": 2,
        "never_decided": [], "approved_not_executed": [], "auto_apply_attempts_by_status": {},
        "report_path": "/x/reports/learning/2026-10-03.md",
    }


def _args(**kw):
    base = dict(as_of=None, lookback_days=None, no_send=False, send_saved=False, pr_url=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr("trading_agent.config.PROCESSED_DATA_DIR", tmp_path)
    sent, built = [], []
    monkeypatch.setattr(
        "trading_agent.reporting.report_builder.build_weekly_learning_review",
        lambda as_of, lb: built.append((as_of, lb)) or _review(),
    )
    monkeypatch.setattr(
        "trading_agent.notify.approval_gateway.notify_weekly_learning_review",
        lambda review, pr_url=None: sent.append((review, pr_url)),
    )
    return tmp_path, sent, built


def test_no_send_writes_a_snapshot_and_sends_nothing(env):
    tmp, sent, _ = env
    cli.cmd_weekly_learning_review(_args(no_send=True))
    assert sent == []
    assert json.loads((tmp / "learning_review_2026-10-03.json").read_text())["trades_count"] == 1


def test_send_saved_emails_the_snapshot_with_the_pr_url_without_recomputing(env, monkeypatch):
    tmp, sent, built = env
    (tmp / "learning_review_2026-10-03.json").write_text(json.dumps(_review()))
    monkeypatch.setattr("trading_agent.utils.today", lambda: "2026-10-03")

    cli.cmd_weekly_learning_review(_args(send_saved=True, pr_url="https://example/pr/1"))

    assert built == []
    assert sent[0][1] == "https://example/pr/1"
    assert sent[0][0]["window_end"] == "2026-10-03"


def test_send_saved_without_a_snapshot_fails_clearly(env, monkeypatch):
    monkeypatch.setattr("trading_agent.utils.today", lambda: "2026-10-03")
    with pytest.raises(SystemExit, match="--no-send first"):
        cli.cmd_weekly_learning_review(_args(send_saved=True))


def test_default_still_builds_and_sends_in_one_step(env):
    _, sent, built = env
    cli.cmd_weekly_learning_review(_args())
    assert len(built) == 1 and len(sent) == 1 and sent[0][1] is None

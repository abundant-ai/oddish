"""PR comment cost/quota rendering: per-trial cost column, run-cost total,
and the daily-quota footer line sourced from the billed user's usage."""

from __future__ import annotations

import asyncio
import importlib
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

formatter = importlib.import_module("oddish.integrations.github.formatter")
notifier = importlib.import_module("oddish.integrations.github.notifier")

QuotaSnapshot = formatter.QuotaSnapshot
TaskSummary = formatter.TaskSummary
TrialSummary = formatter.TrialSummary


def _trial(index: int, *, status: str = "success", cost_usd: float | None = None):
    return TrialSummary(
        index=index,
        trial_id=f"task-1-{index}",
        agent="claude-code",
        model="anthropic/claude-sonnet-4-5",
        status=status,
        reward=1.0 if status == "success" else None,
        duration_seconds=60.0,
        analysis_status=None,
        classification=None,
        cost_usd=cost_usd,
    )


def _task(trials):
    return TaskSummary(
        task_id="task-1",
        task_name="my-task",
        task_url="https://example.test/experiments/exp-1",
        trials=trials,
        verdict_status=None,
        verdict=None,
    )


def test_task_comment_renders_cost_column_and_run_total():
    body = formatter.format_task_comment(
        task=_task([_trial(0, cost_usd=1.25), _trial(1, cost_usd=2.0)]),
        experiment_name="exp",
        experiment_url="https://example.test/experiments/exp-1",
    )
    assert "| Cost |" in body
    assert "$1.25" in body
    assert "$2.00" in body
    assert "**Run cost:** $3.25" in body
    assert "(so far)" not in body


def test_task_comment_marks_partial_run_cost_and_dashes_unpriced():
    body = formatter.format_task_comment(
        task=_task([_trial(0, cost_usd=1.25), _trial(1, status="running")]),
        experiment_name="exp",
        experiment_url="https://example.test/experiments/exp-1",
    )
    assert "**Run cost:** $1.25 (so far)" in body


def test_task_comment_omits_cost_line_when_nothing_priced():
    body = formatter.format_task_comment(
        task=_task([_trial(0, status="running")]),
        experiment_name="exp",
        experiment_url="https://example.test/experiments/exp-1",
    )
    assert "**Run cost:**" not in body
    assert "**Daily quota:**" not in body


def test_task_comment_renders_quota_line():
    body = formatter.format_task_comment(
        task=_task([_trial(0, cost_usd=1.25)]),
        experiment_name="exp",
        experiment_url="https://example.test/experiments/exp-1",
        quota=QuotaSnapshot(used_usd=41.2, limit_usd=100.0),
    )
    assert "**Daily quota:** $41.20 of $100.00 used" in body


def test_experiment_comment_renders_cost_and_quota():
    tasks = [
        _task([_trial(0, cost_usd=1.0)]),
        _task([_trial(0, cost_usd=2.5)]),
    ]
    body = formatter.format_experiment_comment(
        tasks=tasks,
        experiment_name="exp",
        experiment_url="https://example.test/experiments/exp-1",
        quota=QuotaSnapshot(used_usd=10.0, limit_usd=50.0),
    )
    assert "| Cost |" in body
    assert "**Run cost:** $3.50" in body
    assert "**Daily quota:** $10.00 of $50.00 used" in body


# ---------------------------------------------------------------------------
# _get_quota_snapshot gating
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, billed_user_id):
        self._billed_user_id = billed_user_id
        self.scalar_calls = 0
        self.last_stmt = None

    async def scalar(self, stmt):
        self.scalar_calls += 1
        self.last_stmt = stmt
        return self._billed_user_id


def test_quota_snapshot_none_when_mode_off(monkeypatch):
    monkeypatch.setattr(
        notifier.settings, "quota_mode", notifier.QuotaMode.OFF, raising=False
    )
    session = _FakeSession("user-1")
    result = asyncio.run(
        notifier._get_quota_snapshot(
            session, org_id="org-1", task_ids=["task-1"], experiment_id="exp-1"
        )
    )
    assert result is None
    assert session.scalar_calls == 0  # never touches the DB when off


def test_quota_snapshot_none_without_org_or_billed_user(monkeypatch):
    monkeypatch.setattr(
        notifier.settings, "quota_mode", notifier.QuotaMode.SHADOW, raising=False
    )
    assert (
        asyncio.run(
            notifier._get_quota_snapshot(
                _FakeSession("user-1"),
                org_id=None,
                task_ids=["task-1"],
                experiment_id="exp-1",
            )
        )
        is None
    )
    assert (
        asyncio.run(
            notifier._get_quota_snapshot(
                _FakeSession(None),
                org_id="org-1",
                task_ids=["task-1"],
                experiment_id="exp-1",
            )
        )
        is None
    )


def test_quota_snapshot_reads_usage_and_limit(monkeypatch):
    monkeypatch.setattr(
        notifier.settings, "quota_mode", notifier.QuotaMode.SHADOW, raising=False
    )

    async def fake_sum(session, org_id, user_id, period_start):
        assert (org_id, user_id) == ("org-1", "user-1")
        return Decimal("41.2000")

    async def fake_limit(session, org_id, user_id):
        return Decimal("100.00")

    monkeypatch.setattr(notifier, "sum_cost_usd", fake_sum)
    monkeypatch.setattr(notifier, "get_effective_limit", fake_limit)

    session = _FakeSession("user-1")
    result = asyncio.run(
        notifier._get_quota_snapshot(
            session, org_id="org-1", task_ids=["task-1"], experiment_id="exp-1"
        )
    )
    assert result == QuotaSnapshot(used_usd=41.2, limit_usd=100.0)
    # The payer lookup must be scoped to the rendered experiment: a shared
    # task's trials in other experiments (other payers) must not leak in.
    assert "experiment_id" in str(session.last_stmt)


def test_quota_snapshot_swallows_query_errors(monkeypatch):
    monkeypatch.setattr(
        notifier.settings, "quota_mode", notifier.QuotaMode.ENFORCE, raising=False
    )

    class _BoomSession:
        async def scalar(self, _stmt):
            raise RuntimeError("db down")

    result = asyncio.run(
        notifier._get_quota_snapshot(
            _BoomSession(), org_id="org-1", task_ids=["task-1"], experiment_id="exp-1"
        )
    )
    assert result is None

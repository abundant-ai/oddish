"""Every finished agent trial auto-enqueues its own trajectory-summary job.

The durable job itself (``ANALYZER`` + ``payload.mode == "trajectory_summary"``)
already exists; until now the only thing that created one was someone reading a
trajectory, so a trial on a ``run_analysis=False`` task had no summary until a
human asked for it.

The enqueue *shape* -- payload, ``schema_version`` idempotency key, trial lock --
deliberately stays in ``backend``'s ``get_or_enqueue_summary_job``, reached
through the enqueuer seam registered here. Two enqueue sites with two
idempotency keys would each miss the other's job and pay for the same summary
twice, which is the whole reason that key exists.

Fake sessions throughout: nothing here needs a live DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db.models import TrialModel, TrialStatus  # noqa: E402
from oddish.workers.queue import trajectory_summary_job as tsj  # noqa: E402


def _trial(**overrides) -> TrialModel:
    """A trial that is eligible in every respect, before overrides."""
    fields = {
        "id": "trial1",
        "task_id": "task1",
        "task_version_id": "task1-v1",
        "org_id": "org123",
        "agent": "claude-code",
        "status": TrialStatus.SUCCESS,
        "harbor_stage": "verifier",
        "has_trajectory": True,
        "is_probe": False,
        "superseded_by_trial_id": None,
        "finished_at": None,
    }
    fields.update(overrides)
    trial = TrialModel()
    for key, value in fields.items():
        setattr(trial, key, value)
    return trial


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "auto_trajectory_summary", True)


@pytest.fixture
def enqueued(monkeypatch):
    """Install a recording enqueuer; returns the list of trials it received."""
    seen: list = []

    async def _record(_session, trial):
        seen.append(trial)
        return object()

    monkeypatch.setattr(tsj, "_enqueuer", _record)
    return seen


@pytest.mark.asyncio
async def test_finished_agent_trial_is_handed_to_the_enqueuer(enqueued):
    trial = _trial()

    assert await tsj.enqueue_trajectory_summary_job(object(), trial) is not None
    assert enqueued == [trial]


@pytest.mark.asyncio
async def test_flag_off_enqueues_nothing(monkeypatch, enqueued):
    from oddish.config import settings

    monkeypatch.setattr(settings, "auto_trajectory_summary", False)

    assert await tsj.enqueue_trajectory_summary_job(object(), _trial()) is None
    assert enqueued == []


@pytest.mark.asyncio
async def test_unregistered_enqueuer_is_a_no_op(monkeypatch):
    """Standalone oddish has no backend to build a summary; it must not crash."""
    monkeypatch.setattr(tsj, "_enqueuer", None)

    assert await tsj.enqueue_trajectory_summary_job(object(), _trial()) is None


@pytest.mark.parametrize(
    "overrides,why",
    [
        ({"agent": "nop"}, "nop baseline"),
        ({"agent": "oracle"}, "oracle baseline"),
        ({"agent": "ORACLE-v2"}, "suffixed baseline variant"),
        ({"is_probe": True}, "probe trial"),
        ({"has_trajectory": False}, "nothing to summarize"),
        ({"status": TrialStatus.RUNNING}, "not terminal"),
        ({"status": TrialStatus.SKIPPED}, "not terminal"),
        ({"harbor_stage": "cancelled"}, "cancelled mid-run"),
        ({"superseded_by_trial_id": "trial2"}, "superseded by a re-run"),
    ],
)
@pytest.mark.asyncio
async def test_ineligible_trials_enqueue_nothing(overrides, why, enqueued):
    trial = _trial(**overrides)

    assert await tsj.enqueue_trajectory_summary_job(object(), trial) is None, why
    assert enqueued == [], why


@pytest.mark.asyncio
async def test_failed_trial_still_gets_a_summary(enqueued):
    """A FAILED trial is the interesting one to read, so it must qualify."""
    assert (
        await tsj.enqueue_trajectory_summary_job(
            object(), _trial(status=TrialStatus.FAILED)
        )
        is not None
    )


@pytest.mark.asyncio
async def test_grok_build_trial_without_atif_trajectory_qualifies(enqueued):
    """``_has_fetchable_trajectory`` synthesizes ATIF for old grok-build trials."""
    from datetime import datetime, timezone

    trial = _trial(
        agent="grok-build",
        has_trajectory=False,
        finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert await tsj.enqueue_trajectory_summary_job(object(), trial) is not None


@pytest.mark.asyncio
async def test_already_summarized_trial_is_left_to_the_enqueuer(enqueued):
    """Freshness is a schema question, and the seam owns the schema.

    A mirrored ``trials.trajectory_summary`` can be stale (older
    ``schema_version``), and only ``get_or_enqueue_summary_job`` knows the
    current one. Short-circuiting on "has a summary" here would silently skip
    the trials a schema bump is meant to re-summarize.
    """
    trial = _trial()
    trial.trajectory_summary = {"components": [], "schema_version": "1"}

    assert await tsj.enqueue_trajectory_summary_job(object(), trial) is not None
    assert enqueued == [trial]


@pytest.mark.asyncio
async def test_post_trial_hooks_enqueue_the_summary_job(monkeypatch, enqueued):
    from contextlib import asynccontextmanager

    from oddish.db import TaskStatus
    from oddish.workers.queue import trial_handler

    trial = _trial()

    class _Session:
        async def scalar(self, _stmt):
            return trial.task_id

        async def get(self, model, _obj_id, with_for_update=False):
            if model is trial_handler.TaskModel:
                from types import SimpleNamespace

                return SimpleNamespace(status=TaskStatus.RUNNING)
            return trial

    @asynccontextmanager
    async def _fake_get_session():
        yield _Session()

    async def _noop(*_args, **_kwargs):
        return False

    monkeypatch.setattr(trial_handler, "get_session", _fake_get_session)
    monkeypatch.setattr("oddish.queue.maybe_gate_llm_trials", _noop)
    monkeypatch.setattr("oddish.queue.maybe_start_qa_stage", _noop)

    await trial_handler._run_post_trial_hooks(trial.id)

    assert enqueued == [trial]


@pytest.mark.asyncio
async def test_post_trial_hooks_enqueue_nothing_for_a_baseline(monkeypatch, enqueued):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from oddish.db import TaskStatus
    from oddish.workers.queue import trial_handler

    trial = _trial(agent="oracle")

    class _Session:
        async def scalar(self, _stmt):
            return trial.task_id

        async def get(self, model, _obj_id, with_for_update=False):
            if model is trial_handler.TaskModel:
                return SimpleNamespace(status=TaskStatus.RUNNING)
            return trial

    @asynccontextmanager
    async def _fake_get_session():
        yield _Session()

    async def _noop(*_args, **_kwargs):
        return False

    monkeypatch.setattr(trial_handler, "get_session", _fake_get_session)
    monkeypatch.setattr("oddish.queue.maybe_gate_llm_trials", _noop)
    monkeypatch.setattr("oddish.queue.maybe_start_qa_stage", _noop)

    await trial_handler._run_post_trial_hooks(trial.id)

    assert enqueued == []

"""A trial that re-ran must not keep the earlier attempt's classification.

QA is terminal-sticky: once ``analysis_status`` is SUCCESS no later pass
re-classifies the trial. Combined with in-place retries -- which clear the
reward, result and artifacts the classification described -- that pinned an
infra-killed attempt's HARNESS_ERROR onto trials that went on to pass.

The clear happens during settlement rather than at attempt start, so it is
atomic with the QA re-enqueue and cannot leave the trial unclassified for the
length of an attempt.

Runs against a real Postgres (``ODDISH_DATABASE_URL``), like the other queue
tests.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (  # noqa: E402
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobModel,
    get_session,
    utcnow,
)
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402
from oddish.workers.queue import trial_handler  # noqa: E402

_RUN = uuid.uuid4().hex[:8]
_AGENT = "claude-code"
_MODEL = "claude-sonnet-4-5"

_STALE = {"classification": "HARNESS_ERROR", "subtype": "agent_not_started"}


@pytest_asyncio.fixture
async def cleanup_task_ids():
    task_ids: list[str] = []
    yield task_ids
    async with get_session() as session:
        for task_id in task_ids:
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id.like(f"{task_id}%")
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )


async def _make_task(task_id: str) -> str:
    async with get_session() as session:
        await create_task(
            session,
            TaskSubmission(
                name=task_id,
                task_path="s3://test-bucket/stale-analysis-fake-task",
                user="test",
                run_analysis=True,
                trials=[TrialSpec(agent=_AGENT, model=_MODEL)],
            ),
            task_id=task_id,
        )
    async with get_session() as session:
        return await session.scalar(
            select(TrialModel.id).where(TrialModel.task_id == task_id)
        )


async def _settle(
    trial_id: str,
    *,
    attempts: int,
    is_probe: bool = False,
    task_status: TaskStatus,
) -> None:
    """Put the trial in the state the settle hook sees after a re-attempt."""
    started = utcnow()
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        trial.status = TrialStatus.SUCCESS
        trial.reward = 1.0
        trial.attempts = attempts
        trial.started_at = started
        trial.finished_at = started + timedelta(minutes=45)
        trial.analysis = _STALE
        trial.analysis_status = AnalysisStatus.SUCCESS
        trial.analysis_log = "old log"
        trial.analysis_started_at = started - timedelta(hours=4)
        trial.analysis_finished_at = started - timedelta(hours=4)
        trial.is_probe = is_probe

        task = await session.get(TaskModel, trial.task_id)
        task.status = task_status
        task.verdict = {"is_good": True}
        task.verdict_finished_at = started
        if task_status is TaskStatus.COMPLETED:
            task.finished_at = started


@pytest.mark.asyncio
async def test_stale_analysis_is_dropped_and_the_closed_task_reopens(
    cleanup_task_ids, monkeypatch
):
    """The regression: a passing trial keeps an earlier attempt's HARNESS_ERROR.

    The task had already closed out, so nothing would have re-enqueued QA
    either -- both halves have to give for the trial to be reclassified.
    """
    task_id = f"stale-analysis-reopen-{_RUN}"
    cleanup_task_ids.append(task_id)
    trial_id = await _make_task(task_id)
    await _settle(
        trial_id,
        attempts=4,
        task_status=TaskStatus.COMPLETED,
    )

    await trial_handler._run_post_trial_hooks(trial_id)

    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        task = await session.get(TaskModel, task_id)

    assert trial.analysis is None
    assert trial.analysis_status is None
    assert trial.analysis_finished_at is None
    assert trial.analysis_log is None
    # Reopened, so maybe_start_qa_stage can run again.
    assert task.status is not TaskStatus.COMPLETED
    assert task.verdict is None
    assert task.finished_at is None


@pytest.mark.asyncio
async def test_a_probes_own_classification_survives_settlement(cleanup_task_ids):
    """A probe writes its analysis DURING settlement; it must not be wiped.

    Keying on ``attempts > 1`` alone would destroy it on any retried probe.
    """
    task_id = f"stale-analysis-probe-{_RUN}"
    cleanup_task_ids.append(task_id)
    trial_id = await _make_task(task_id)
    await _settle(
        trial_id,
        attempts=3,
        is_probe=True,
        task_status=TaskStatus.COMPLETED,
    )

    await trial_handler._run_post_trial_hooks(trial_id)

    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)

    assert trial.analysis == _STALE
    assert trial.analysis_status is AnalysisStatus.SUCCESS


@pytest.mark.asyncio
async def test_a_first_attempt_analysis_is_left_alone(cleanup_task_ids):
    """Nothing re-ran, so the classification still describes the live result."""
    task_id = f"stale-analysis-first-{_RUN}"
    cleanup_task_ids.append(task_id)
    trial_id = await _make_task(task_id)
    await _settle(
        trial_id,
        attempts=1,
        task_status=TaskStatus.COMPLETED,
    )

    await trial_handler._run_post_trial_hooks(trial_id)

    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        task = await session.get(TaskModel, task_id)

    assert trial.analysis == _STALE
    assert task.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_label_written_mid_attempt_is_dropped_too(cleanup_task_ids):
    """An in-flight QA job can classify a trial while its attempt is running.

    ``_prepare_trial_run`` wipes ``trial_s3_key``/``result`` at attempt start, so
    such a label is derived from an empty row -- and it carries a stamp NEWER
    than the attempt's own start, which is why the clear cannot key on
    timestamps.
    """
    task_id = f"stale-analysis-midflight-{_RUN}"
    cleanup_task_ids.append(task_id)
    trial_id = await _make_task(task_id)
    await _settle(trial_id, attempts=2, task_status=TaskStatus.COMPLETED)
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        # Classified after this attempt began, but before it settled.
        mid = trial.started_at + timedelta(minutes=5)
        trial.analysis_started_at = mid
        trial.analysis_finished_at = mid

    await trial_handler._run_post_trial_hooks(trial_id)

    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)

    assert trial.analysis is None
    assert trial.analysis_status is None

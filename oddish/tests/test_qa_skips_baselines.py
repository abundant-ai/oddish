"""QA never runs on nop/oracle baselines.

Two independent guards, one per path that would otherwise spend on a
deterministic baseline: the per-trial classifier and the task-level QA
stage transition.

Runs against a real Postgres (``ODDISH_DATABASE_URL``), like the other queue
tests.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (  # noqa: E402
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobKind,
    WorkerJobModel,
    get_session,
)
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402
from oddish.workers.queue.qa_handler import (  # noqa: E402
    _load_live_trials_for_classification,
)

_RUN = uuid.uuid4().hex[:8]
_LLM_AGENT = "claude-code"
_LLM_MODEL = "claude-sonnet-4-5"

# Suffixed/prefixed variants must be caught too -- an "oracle-v2" is still a
# baseline, and matching only the bare names would silently keep paying for it.
_BASELINE_AGENTS = ("nop", "oracle", "oracle-v2", "agent-nop", "NOP")


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


def _submission(
    name: str,
    agents: list[tuple[str, str | None]],
    *,
    run_analysis: bool = False,
) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/qa-skip-fake-task",
        user="test",
        run_analysis=run_analysis,
        trials=[TrialSpec(agent=a, model=m) for a, m in agents],
    )


async def _finish_all_trials(task_id: str) -> None:
    """Mark every trial SUCCESS so it is otherwise QA-eligible."""
    async with get_session() as session:
        trials = (
            (
                await session.execute(
                    select(TrialModel).where(TrialModel.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
        for trial in trials:
            trial.status = TrialStatus.SUCCESS


@pytest.mark.asyncio
async def test_classifier_skips_baselines_including_variants(cleanup_task_ids):
    """The QA classifier loads the LLM trial and none of the baselines."""
    task_id = f"qa-skip-cls-{_RUN}"
    cleanup_task_ids.append(task_id)
    agents = [(a, None) for a in _BASELINE_AGENTS] + [(_LLM_AGENT, _LLM_MODEL)]
    async with get_session() as session:
        await create_task(session, _submission("cls", agents), task_id=task_id)
    await _finish_all_trials(task_id)

    async with get_session() as session:
        agent_by_id = dict(
            (
                await session.execute(
                    select(TrialModel.id, TrialModel.agent).where(
                        TrialModel.task_id == task_id
                    )
                )
            ).all()
        )

    live_ids = {tid for tid, _ in await _load_live_trials_for_classification(task_id)}

    assert [agent_by_id[tid] for tid in live_ids] == [_LLM_AGENT]
    # Named individually so a regression says WHICH variant leaked through.
    for tid, agent in agent_by_id.items():
        if agent != _LLM_AGENT:
            assert tid not in live_ids, f"baseline {agent!r} was queued for QA"


@pytest.mark.asyncio
async def test_baseline_only_task_enqueues_no_qa_job(cleanup_task_ids):
    """``maybe_start_qa_stage``'s eligibility count mirrors the classifier's.

    A task whose only live trials are baselines has nothing left to classify, so
    it must complete rather than enqueue a QA job that could only no-op. These
    two filters drifting apart is the failure this guards.
    """
    from oddish.queue import maybe_start_qa_stage

    task_id = f"qa-skip-stage-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session,
            _submission("stage", [("nop", None), ("oracle", None)], run_analysis=True),
            task_id=task_id,
        )
    await _finish_all_trials(task_id)

    async with get_session() as session:
        trial_id = (
            await session.execute(
                select(TrialModel.id).where(TrialModel.task_id == task_id).limit(1)
            )
        ).scalar_one()
        assert await maybe_start_qa_stage(session, trial_id) is True

    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        qa_jobs = (
            (
                await session.execute(
                    select(WorkerJobModel.id).where(
                        WorkerJobModel.kind == WorkerJobKind.QA,
                        WorkerJobModel.subject_id == task_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert qa_jobs == [], "a baseline-only task enqueued a QA job with nothing to QA"
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict_status is None


@pytest.mark.asyncio
async def test_qa_stage_ignores_historical_version_eligible_trials(cleanup_task_ids):
    """A historical LLM trial cannot enqueue QA for an empty current version."""
    from oddish.queue import maybe_start_qa_stage

    task_id = f"qa-version-stage-old-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        task = await create_task(
            session,
            _submission(
                "version-stage-old",
                [(_LLM_AGENT, _LLM_MODEL)],
                run_analysis=True,
            ),
            task_id=task_id,
        )
        historical_trial_id = task.trials[0].id
        current_version_id = f"{task_id}-v2"
        session.add(
            TaskVersionModel(
                id=current_version_id,
                task_id=task_id,
                version=2,
                task_path=f"s3://test-bucket/{task_id}/v2",
            )
        )
        await session.flush()
        task.current_version_id = current_version_id

    await _finish_all_trials(task_id)

    async with get_session() as session:
        assert await maybe_start_qa_stage(session, historical_trial_id) is True

    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        qa_jobs = (
            (
                await session.execute(
                    select(WorkerJobModel.id).where(
                        WorkerJobModel.kind == WorkerJobKind.QA,
                        WorkerJobModel.subject_id == task_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert qa_jobs == []
    assert task.status == TaskStatus.COMPLETED
    assert task.verdict_status is None


@pytest.mark.asyncio
async def test_qa_stage_enqueues_for_current_version_eligible_trial(cleanup_task_ids):
    """A current-version LLM trial still enqueues the task QA job."""
    from oddish.queue import maybe_start_qa_stage

    task_id = f"qa-version-stage-current-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        task = await create_task(
            session,
            _submission(
                "version-stage-current",
                [(_LLM_AGENT, _LLM_MODEL)],
                run_analysis=True,
            ),
            task_id=task_id,
        )
        current_trial_id = task.trials[0].id

    await _finish_all_trials(task_id)

    async with get_session() as session:
        assert await maybe_start_qa_stage(session, current_trial_id) is True

    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        qa_jobs = (
            (
                await session.execute(
                    select(WorkerJobModel.id).where(
                        WorkerJobModel.kind == WorkerJobKind.QA,
                        WorkerJobModel.subject_id == task_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(qa_jobs) == 1
    assert task.status == TaskStatus.VERDICT_PENDING
    assert task.verdict_status == VerdictStatus.QUEUED


@pytest.mark.asyncio
async def test_qa_stage_historical_active_trial_does_not_block_current_version(
    cleanup_task_ids,
):
    from oddish.queue import maybe_start_qa_stage

    task_id = f"qa-version-active-old-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        task = await create_task(
            session,
            _submission(
                "version-active-old",
                [(_LLM_AGENT, _LLM_MODEL), (_LLM_AGENT, _LLM_MODEL)],
                run_analysis=True,
            ),
            task_id=task_id,
        )
        historical_trial, current_trial = task.trials
        current_version_id = f"{task_id}-v2"
        session.add(
            TaskVersionModel(
                id=current_version_id,
                task_id=task_id,
                version=2,
                task_path=f"s3://test-bucket/{task_id}/v2",
            )
        )
        await session.flush()
        current_trial.task_version_id = current_version_id
        task.current_version_id = current_version_id
        historical_trial.status = TrialStatus.RUNNING
        current_trial.status = TrialStatus.SUCCESS
        current_trial_id = current_trial.id

    async with get_session() as session:
        assert await maybe_start_qa_stage(session, current_trial_id) is True

    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        qa_jobs = (
            (
                await session.execute(
                    select(WorkerJobModel.id).where(
                        WorkerJobModel.kind == WorkerJobKind.QA,
                        WorkerJobModel.subject_id == task_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(qa_jobs) == 1
    assert task.status == TaskStatus.VERDICT_PENDING


@pytest.mark.asyncio
async def test_qa_stage_current_active_trial_still_blocks_current_version(
    cleanup_task_ids,
):
    from oddish.queue import maybe_start_qa_stage

    task_id = f"qa-version-active-current-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        task = await create_task(
            session,
            _submission(
                "version-active-current",
                [(_LLM_AGENT, _LLM_MODEL), (_LLM_AGENT, _LLM_MODEL)],
                run_analysis=True,
            ),
            task_id=task_id,
        )
        historical_trial, current_trial = task.trials
        current_version_id = f"{task_id}-v2"
        session.add(
            TaskVersionModel(
                id=current_version_id,
                task_id=task_id,
                version=2,
                task_path=f"s3://test-bucket/{task_id}/v2",
            )
        )
        await session.flush()
        current_trial.task_version_id = current_version_id
        task.current_version_id = current_version_id
        historical_trial.status = TrialStatus.SUCCESS
        current_trial.status = TrialStatus.RUNNING
        current_trial_id = current_trial.id

    async with get_session() as session:
        assert await maybe_start_qa_stage(session, current_trial_id) is False

    async with get_session() as session:
        qa_jobs = (
            (
                await session.execute(
                    select(WorkerJobModel.id).where(
                        WorkerJobModel.kind == WorkerJobKind.QA,
                        WorkerJobModel.subject_id == task_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert qa_jobs == []

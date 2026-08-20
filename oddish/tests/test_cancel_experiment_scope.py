"""Cancel must not cross experiment boundaries for shared tasks."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
)
from oddish.queue import cancel_tasks_runs  # noqa: E402


@pytest.mark.asyncio
async def test_cancel_tasks_runs_is_experiment_scoped(session):
    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-cxlscope-{suffix}"
    exp_a = f"exp-a-{suffix}"
    exp_b = f"exp-b-{suffix}"
    task_id = f"task-cxlscope-{suffix}"
    trial_a = f"{task_id}-a"
    trial_b = f"{task_id}-b"

    session.add_all(
        [
            ExperimentModel(id=exp_a, name=exp_a, org_id=org_id),
            ExperimentModel(id=exp_b, name=exp_b, org_id=org_id),
            TaskModel(
                id=task_id,
                name=task_id,
                org_id=org_id,
                user="tester",
                task_path="s3://test-bucket/cancel-scope-task",
                status=TaskStatus.RUNNING,
            ),
            TrialModel(
                id=trial_a,
                name=trial_a,
                task_id=task_id,
                experiment_id=exp_a,
                org_id=org_id,
                agent="codex",
                provider="openai",
                queue_key="openai/gpt-5",
                model="gpt-5",
                is_probe=False,
                max_attempts=6,
                attempts=1,
                status=TrialStatus.RUNNING,
            ),
            TrialModel(
                id=trial_b,
                name=trial_b,
                task_id=task_id,
                experiment_id=exp_b,
                org_id=org_id,
                agent="codex",
                provider="openai",
                queue_key="openai/gpt-5",
                model="gpt-5",
                is_probe=False,
                max_attempts=6,
                attempts=1,
                status=TrialStatus.RUNNING,
            ),
            WorkerJobModel(
                kind=WorkerJobKind.TRIAL,
                status=WorkerJobStatus.RUNNING,
                queue_key="openai/gpt-5",
                subject_table="trials",
                subject_id=trial_a,
                modal_function_call_id="fc-a",
                provider="daytona",
                external_id="sb-a",
            ),
            WorkerJobModel(
                kind=WorkerJobKind.TRIAL,
                status=WorkerJobStatus.RUNNING,
                queue_key="openai/gpt-5",
                subject_table="trials",
                subject_id=trial_b,
                modal_function_call_id="fc-b",
                provider="daytona",
                external_id="sb-b",
            ),
            WorkerJobModel(
                kind=WorkerJobKind.QA,
                status=WorkerJobStatus.QUEUED,
                queue_key="analysis",
                subject_table="tasks",
                subject_id=task_id,
                modal_function_call_id="fc-qa",
            ),
        ]
    )
    await session.commit()

    try:
        result = await cancel_tasks_runs(
            session,
            [task_id],
            org_id=org_id,
            experiment_id=exp_a,
        )

        assert result["trials_cancelled"] == 1
        assert result["modal_function_call_ids"] == ["fc-a"]
        assert result["worker_targets"] == [("daytona", "sb-a")]

        trial_rows = (
            await session.execute(
                select(TrialModel).where(TrialModel.task_id == task_id)
            )
        ).scalars()
        by_id = {trial.id: trial for trial in trial_rows}
        assert by_id[trial_a].status == TrialStatus.FAILED
        assert by_id[trial_a].error_message == "Cancelled by user"
        assert by_id[trial_b].status == TrialStatus.RUNNING
        assert by_id[trial_b].error_message is None

        job_rows = (
            await session.execute(
                select(WorkerJobModel).where(
                    WorkerJobModel.subject_id.in_([trial_a, trial_b, task_id])
                )
            )
        ).scalars()
        by_subject = {job.subject_id: job for job in job_rows}
        assert by_subject[trial_a].status == WorkerJobStatus.CANCELLED
        assert by_subject[trial_b].status == WorkerJobStatus.RUNNING
        assert by_subject[task_id].status == WorkerJobStatus.QUEUED

        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == TaskStatus.RUNNING
    finally:
        await session.rollback()
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id.in_([trial_a, trial_b, task_id])
            )
        )
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id.in_([exp_a, exp_b])
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_cancel_tasks_runs_without_experiment_still_cancels_all(session):
    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-cxlall-{suffix}"
    exp_a = f"exp-a-{suffix}"
    exp_b = f"exp-b-{suffix}"
    task_id = f"task-cxlall-{suffix}"
    trial_a = f"{task_id}-a"
    trial_b = f"{task_id}-b"

    session.add_all(
        [
            ExperimentModel(id=exp_a, name=exp_a, org_id=org_id),
            ExperimentModel(id=exp_b, name=exp_b, org_id=org_id),
            TaskModel(
                id=task_id,
                name=task_id,
                org_id=org_id,
                user="tester",
                task_path="s3://test-bucket/cancel-all-task",
                status=TaskStatus.RUNNING,
            ),
            TrialModel(
                id=trial_a,
                name=trial_a,
                task_id=task_id,
                experiment_id=exp_a,
                org_id=org_id,
                agent="codex",
                provider="openai",
                queue_key="openai/gpt-5",
                model="gpt-5",
                is_probe=False,
                max_attempts=6,
                attempts=1,
                status=TrialStatus.RUNNING,
            ),
            TrialModel(
                id=trial_b,
                name=trial_b,
                task_id=task_id,
                experiment_id=exp_b,
                org_id=org_id,
                agent="codex",
                provider="openai",
                queue_key="openai/gpt-5",
                model="gpt-5",
                is_probe=False,
                max_attempts=6,
                attempts=1,
                status=TrialStatus.RUNNING,
            ),
            WorkerJobModel(
                kind=WorkerJobKind.TRIAL,
                status=WorkerJobStatus.RUNNING,
                queue_key="openai/gpt-5",
                subject_table="trials",
                subject_id=trial_a,
            ),
            WorkerJobModel(
                kind=WorkerJobKind.TRIAL,
                status=WorkerJobStatus.RUNNING,
                queue_key="openai/gpt-5",
                subject_table="trials",
                subject_id=trial_b,
            ),
        ]
    )
    await session.commit()

    try:
        result = await cancel_tasks_runs(session, [task_id], org_id=org_id)
        assert result["trials_cancelled"] == 2

        statuses = (
            (
                await session.execute(
                    select(TrialModel.status).where(TrialModel.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
        assert set(statuses) == {TrialStatus.FAILED}

        task = (
            await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        ).scalar_one()
        assert task.status == TaskStatus.FAILED
    finally:
        await session.rollback()
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id.in_([trial_a, trial_b, task_id])
            )
        )
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id.in_([exp_a, exp_b])
            )
        )
        await session.commit()

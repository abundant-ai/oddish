from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from oddish.config import QuotaMode, settings
from oddish.core import quota_enforcement
from oddish.core.quota_enforcement import (
    QUOTA_CANCELLED_MESSAGE,
    cancel_trials_if_quota_reached,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
)


def _trial(
    *,
    trial_id: str,
    task_id: str,
    experiment_id: str,
    org_id: str,
    billed_user_id: str,
    status: TrialStatus,
    cost_usd: float | None = None,
    finished_at: datetime | None = None,
) -> TrialModel:
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task_id,
        experiment_id=experiment_id,
        org_id=org_id,
        billed_user_id=billed_user_id,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5",
        model="gpt-5",
        is_probe=False,
        attempts=1,
        max_attempts=6,
        status=status,
        cost_usd=cost_usd,
        finished_at=finished_at,
    )


def _job(
    *,
    subject_table: str,
    subject_id: str,
    kind: WorkerJobKind,
    modal_id: str,
) -> WorkerJobModel:
    return WorkerJobModel(
        kind=kind,
        status=WorkerJobStatus.RUNNING,
        queue_key="openai/gpt-5",
        subject_table=subject_table,
        subject_id=subject_id,
        modal_function_call_id=modal_id,
        provider="daytona",
        external_id=f"sandbox-{modal_id}",
    )


@pytest.fixture(autouse=True)
def _enforced_quota(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0"))


@pytest.mark.asyncio
async def test_user_quota_cancels_only_payers_trials_and_exhausted_tasks(
    session, monkeypatch
):
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org-qc-{suffix}"
    user_id = f"user-qc-{suffix}"
    other_user_id = f"other-qc-{suffix}"
    experiment_id = f"exp-qc-{suffix}"
    exhausted_task_id = f"task-exhausted-qc-{suffix}"
    mixed_task_id = f"task-mixed-qc-{suffix}"
    now = datetime.now(timezone.utc)

    async def no_org_limit(*_args):
        return None

    async def user_limit(*_args):
        return Decimal("0.30")

    monkeypatch.setattr(quota_enforcement, "get_effective_org_limit", no_org_limit)
    monkeypatch.setattr(quota_enforcement, "get_effective_limit", user_limit)

    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    for task_id in (exhausted_task_id, mixed_task_id):
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                org_id=org_id,
                user="tester",
                task_path="s3://test-bucket/quota-cancel-task",
                status=TaskStatus.RUNNING,
            )
        )

    settled_id = f"{exhausted_task_id}-0"
    exhausted_id = f"{exhausted_task_id}-1"
    mixed_target_id = f"{mixed_task_id}-0"
    mixed_other_id = f"{mixed_task_id}-1"
    session.add_all(
        [
            _trial(
                trial_id=settled_id,
                task_id=exhausted_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=user_id,
                status=TrialStatus.SUCCESS,
                cost_usd=0.30,
                finished_at=now,
            ),
            _trial(
                trial_id=exhausted_id,
                task_id=exhausted_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=user_id,
                status=TrialStatus.RUNNING,
            ),
            _trial(
                trial_id=mixed_target_id,
                task_id=mixed_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=user_id,
                status=TrialStatus.RUNNING,
            ),
            _trial(
                trial_id=mixed_other_id,
                task_id=mixed_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=other_user_id,
                status=TrialStatus.RUNNING,
            ),
        ]
    )
    exhausted_job = _job(
        subject_table="trials",
        subject_id=exhausted_id,
        kind=WorkerJobKind.TRIAL,
        modal_id="fc-exhausted",
    )
    mixed_target_job = _job(
        subject_table="trials",
        subject_id=mixed_target_id,
        kind=WorkerJobKind.TRIAL,
        modal_id="fc-mixed-target",
    )
    mixed_other_job = _job(
        subject_table="trials",
        subject_id=mixed_other_id,
        kind=WorkerJobKind.TRIAL,
        modal_id="fc-mixed-other",
    )
    qa_job = _job(
        subject_table="tasks",
        subject_id=exhausted_task_id,
        kind=WorkerJobKind.QA,
        modal_id="fc-qa",
    )
    session.add_all([exhausted_job, mixed_target_job, mixed_other_job, qa_job])
    await session.flush()

    result = await cancel_trials_if_quota_reached(
        session, org_id=org_id, billed_user_id=user_id
    )
    for job in (exhausted_job, mixed_target_job, mixed_other_job, qa_job):
        await session.refresh(job)

    assert result["scope"] == "user"
    assert result["trials_cancelled"] == 2
    assert result["tasks_cancelled"] == 1
    assert set(result["modal_function_call_ids"]) == {
        "fc-exhausted",
        "fc-mixed-target",
        "fc-qa",
    }
    assert (await session.get(TrialModel, exhausted_id)).error_message == (
        QUOTA_CANCELLED_MESSAGE
    )
    assert (await session.get(TrialModel, mixed_target_id)).status == TrialStatus.FAILED
    assert (await session.get(TrialModel, mixed_other_id)).status == TrialStatus.RUNNING
    assert (await session.get(TaskModel, exhausted_task_id)).status == TaskStatus.FAILED
    assert (await session.get(TaskModel, mixed_task_id)).status == TaskStatus.RUNNING
    assert (await session.get(WorkerJobModel, exhausted_job.id)).status == (
        WorkerJobStatus.CANCELLED
    )
    assert (await session.get(WorkerJobModel, mixed_target_job.id)).status == (
        WorkerJobStatus.CANCELLED
    )
    assert (await session.get(WorkerJobModel, qa_job.id)).status == (
        WorkerJobStatus.CANCELLED
    )
    assert (await session.get(WorkerJobModel, mixed_other_job.id)).status == (
        WorkerJobStatus.RUNNING
    )


@pytest.mark.asyncio
async def test_org_quota_cancels_every_users_active_trials(session, monkeypatch):
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org-org-qc-{suffix}"
    experiment_id = f"exp-org-qc-{suffix}"
    task_id = f"task-org-qc-{suffix}"
    now = datetime.now(timezone.utc)

    async def org_limit(*_args):
        return Decimal("0.30")

    monkeypatch.setattr(quota_enforcement, "get_effective_org_limit", org_limit)
    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="tester",
            task_path="s3://test-bucket/org-quota-cancel-task",
            status=TaskStatus.RUNNING,
        )
    )
    session.add_all(
        [
            _trial(
                trial_id=f"{task_id}-0",
                task_id=task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id="user-a",
                status=TrialStatus.SUCCESS,
                cost_usd=0.30,
                finished_at=now,
            ),
            _trial(
                trial_id=f"{task_id}-1",
                task_id=task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id="user-a",
                status=TrialStatus.RUNNING,
            ),
            _trial(
                trial_id=f"{task_id}-2",
                task_id=task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id="user-b",
                status=TrialStatus.QUEUED,
            ),
        ]
    )
    await session.flush()

    result = await cancel_trials_if_quota_reached(
        session, org_id=org_id, billed_user_id="user-a"
    )

    assert result["scope"] == "org"
    assert result["trials_cancelled"] == 2
    assert (await session.get(TrialModel, f"{task_id}-1")).status == TrialStatus.FAILED
    assert (await session.get(TrialModel, f"{task_id}-2")).status == TrialStatus.FAILED


@pytest.mark.asyncio
async def test_shadow_mode_does_not_cancel(session, monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    result = await cancel_trials_if_quota_reached(
        session, org_id="org-shadow", billed_user_id="user-shadow"
    )
    assert result["trials_cancelled"] == 0
    assert result["scope"] is None

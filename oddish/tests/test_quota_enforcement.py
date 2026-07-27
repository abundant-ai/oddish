from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from oddish.config import QuotaMode, settings
from oddish.core import quota_enforcement
from oddish.core.quota_enforcement import (
    QUOTA_CANCELLED_MESSAGE,
    cancel_trials_if_quota_reached,
    enforce_trial_quotas,
    enforce_trial_quotas_until_checked,
)
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
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
async def test_user_quota_cancels_payer_trials_and_advances_preserved_tasks(
    session, monkeypatch
):
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org-qc-{suffix}"
    user_id = f"user-qc-{suffix}"
    other_user_id = f"other-qc-{suffix}"
    experiment_id = f"exp-qc-{suffix}"
    exhausted_task_id = f"task-exhausted-qc-{suffix}"
    mixed_task_id = f"task-mixed-qc-{suffix}"
    advance_task_id = f"task-advance-qc-{suffix}"
    now = datetime.now(timezone.utc)

    async def no_org_limit(*_args):
        return None

    async def user_limit(*_args):
        return Decimal("0.30")

    monkeypatch.setattr(quota_enforcement, "get_effective_org_limit", no_org_limit)
    monkeypatch.setattr(quota_enforcement, "get_effective_limit", user_limit)

    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    for task_id in (exhausted_task_id, mixed_task_id, advance_task_id):
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                org_id=org_id,
                user="tester",
                task_path="s3://test-bucket/quota-cancel-task",
                status=(
                    TaskStatus.VERDICT_PENDING
                    if task_id == mixed_task_id
                    else TaskStatus.RUNNING
                ),
                verdict_status=(
                    VerdictStatus.RUNNING if task_id == mixed_task_id else None
                ),
                run_analysis=task_id == advance_task_id,
            )
        )

    settled_id = f"{exhausted_task_id}-0"
    exhausted_id = f"{exhausted_task_id}-1"
    mixed_target_id = f"{mixed_task_id}-0"
    mixed_other_id = f"{mixed_task_id}-1"
    advance_target_id = f"{advance_task_id}-0"
    advance_other_id = f"{advance_task_id}-1"
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
                status=TrialStatus.SUCCESS,
                finished_at=now,
            ),
            _trial(
                trial_id=advance_target_id,
                task_id=advance_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=user_id,
                status=TrialStatus.RUNNING,
            ),
            _trial(
                trial_id=advance_other_id,
                task_id=advance_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=other_user_id,
                status=TrialStatus.SUCCESS,
                finished_at=now,
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
    mixed_qa_job = _job(
        subject_table="tasks",
        subject_id=mixed_task_id,
        kind=WorkerJobKind.QA,
        modal_id="fc-mixed-qa",
    )
    session.add_all(
        [exhausted_job, mixed_target_job, mixed_other_job, qa_job, mixed_qa_job]
    )
    await session.flush()

    result = await cancel_trials_if_quota_reached(
        session,
        org_id=org_id,
        billed_user_id=user_id,
        caller_trial_id=exhausted_id,
    )
    for job in (
        exhausted_job,
        mixed_target_job,
        mixed_other_job,
        qa_job,
        mixed_qa_job,
    ):
        await session.refresh(job)

    assert result["scope"] == "user"
    assert result["trials_cancelled"] == 3
    assert result["tasks_cancelled"] == 1
    assert set(result["modal_function_call_ids"]) == {
        "fc-mixed-target",
        "fc-qa",
    }
    assert result["caller_modal_function_call_ids"] == ["fc-exhausted"]
    assert (await session.get(TrialModel, exhausted_id)).error_message == (
        QUOTA_CANCELLED_MESSAGE
    )
    assert (await session.get(TrialModel, mixed_target_id)).status == TrialStatus.FAILED
    assert (await session.get(TrialModel, mixed_target_id)).analysis_status == (
        AnalysisStatus.FAILED
    )
    assert (await session.get(TrialModel, mixed_other_id)).status == TrialStatus.SUCCESS
    assert (await session.get(TaskModel, exhausted_task_id)).status == TaskStatus.FAILED
    mixed_task = await session.get(TaskModel, mixed_task_id)
    assert mixed_task.status == TaskStatus.VERDICT_PENDING
    assert mixed_task.verdict_status == VerdictStatus.RUNNING
    advance_task = await session.get(TaskModel, advance_task_id)
    assert advance_task.status == TaskStatus.VERDICT_PENDING
    assert advance_task.verdict_status == VerdictStatus.QUEUED
    qa_jobs = (
        await session.scalars(
            select(WorkerJobModel).where(
                WorkerJobModel.subject_table == "tasks",
                WorkerJobModel.subject_id == advance_task_id,
                WorkerJobModel.kind == WorkerJobKind.QA,
            )
        )
    ).all()
    assert len(qa_jobs) == 1
    assert qa_jobs[0].status == WorkerJobStatus.QUEUED
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
    assert (await session.get(WorkerJobModel, mixed_qa_job.id)).status == (
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
    assert (await session.get(TaskModel, task_id)).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_shadow_mode_does_not_cancel(session, monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    result = await cancel_trials_if_quota_reached(
        session, org_id="org-shadow", billed_user_id="user-shadow"
    )
    assert result["trials_cancelled"] == 0
    assert result["scope"] is None


@pytest.mark.asyncio
async def test_enforcement_terminates_callers_modal_worker_last(monkeypatch):
    @asynccontextmanager
    async def fake_get_session():
        yield object()

    async def fake_cancel(*_args, **_kwargs):
        return {
            "scope": "user",
            "trials_cancelled": 2,
            "tasks_cancelled": 1,
            "modal_function_call_ids": ["fc-sibling"],
            "caller_modal_function_call_ids": ["fc-caller"],
            "worker_targets": [("daytona", "sandbox-caller")],
        }

    termination_calls = []

    async def fake_terminate(result):
        termination_calls.append(dict(result))
        return len(result["modal_function_call_ids"])

    monkeypatch.setattr(quota_enforcement, "get_session", fake_get_session)
    monkeypatch.setattr(
        quota_enforcement, "cancel_trials_if_quota_reached", fake_cancel
    )
    monkeypatch.setattr(quota_enforcement, "terminate_run_harvest", fake_terminate)

    cancelled = await enforce_trial_quotas(
        org_id="org-1", billed_user_id="user-1", caller_trial_id="trial-1"
    )

    assert cancelled == 2
    assert termination_calls == [
        {
            "modal_function_call_ids": ["fc-sibling"],
            "worker_targets": [("daytona", "sandbox-caller")],
        },
        {"modal_function_call_ids": ["fc-caller"], "worker_targets": []},
    ]


@pytest.mark.asyncio
async def test_enforcement_retries_harvested_handles_before_ack(monkeypatch):
    @asynccontextmanager
    async def fake_get_session():
        yield object()

    async def fake_cancel(*_args, **_kwargs):
        return {
            "scope": "user",
            "trials_cancelled": 2,
            "tasks_cancelled": 1,
            "modal_function_call_ids": ["fc-sibling"],
            "caller_modal_function_call_ids": ["fc-caller"],
            "worker_targets": [("daytona", "sandbox-sibling")],
        }

    termination_calls = []
    sleeps = []
    sibling_failures = 7
    caller_failures = 1

    async def flaky_terminate(result):
        nonlocal sibling_failures, caller_failures
        termination_calls.append(
            {
                "modal_function_call_ids": list(result["modal_function_call_ids"]),
                "worker_targets": list(result["worker_targets"]),
            }
        )
        caller = result["modal_function_call_ids"] == ["fc-caller"]
        should_fail = caller_failures if caller else sibling_failures
        result.pop("modal_function_call_ids")
        result.pop("worker_targets")
        if should_fail:
            if caller:
                caller_failures -= 1
            else:
                sibling_failures -= 1
            raise RuntimeError("temporary teardown failure")

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(quota_enforcement, "get_session", fake_get_session)
    monkeypatch.setattr(
        quota_enforcement, "cancel_trials_if_quota_reached", fake_cancel
    )
    monkeypatch.setattr(quota_enforcement, "terminate_run_harvest", flaky_terminate)
    monkeypatch.setattr(quota_enforcement.asyncio, "sleep", record_sleep)

    assert (
        await enforce_trial_quotas(
            org_id="org-1", billed_user_id="user-1", caller_trial_id="trial-1"
        )
        == 2
    )
    sibling_harvest = {
        "modal_function_call_ids": ["fc-sibling"],
        "worker_targets": [("daytona", "sandbox-sibling")],
    }
    caller_harvest = {
        "modal_function_call_ids": ["fc-caller"],
        "worker_targets": [],
    }
    assert termination_calls == [sibling_harvest] * 8 + [caller_harvest] * 2
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 1.0]


@pytest.mark.asyncio
async def test_enforcement_failure_is_reported_for_live_tail_retry(monkeypatch):
    @asynccontextmanager
    async def fake_get_session():
        yield object()

    async def fail_cancel(*_args, **_kwargs):
        raise RuntimeError("temporary database failure")

    monkeypatch.setattr(quota_enforcement, "get_session", fake_get_session)
    monkeypatch.setattr(
        quota_enforcement, "cancel_trials_if_quota_reached", fail_cancel
    )

    assert (
        await enforce_trial_quotas(
            org_id="org-1", billed_user_id="user-1", caller_trial_id="trial-1"
        )
        is None
    )


@pytest.mark.asyncio
async def test_settlement_enforcement_retries_until_check_succeeds(monkeypatch):
    outcomes = [None] * 7 + [2]
    sleeps = []

    async def enforce_once(**_kwargs):
        return outcomes.pop(0)

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(quota_enforcement, "enforce_trial_quotas", enforce_once)
    monkeypatch.setattr(quota_enforcement.asyncio, "sleep", record_sleep)

    assert (
        await enforce_trial_quotas_until_checked(
            org_id="org-1", billed_user_id="user-1", caller_trial_id="trial-1"
        )
        == 2
    )
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


@pytest.mark.asyncio
async def test_settlement_retry_skips_when_quota_is_not_enforced(monkeypatch):
    async def unexpected_check(**_kwargs):
        raise AssertionError("quota check should be skipped")

    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    monkeypatch.setattr(quota_enforcement, "enforce_trial_quotas", unexpected_check)

    assert (
        await enforce_trial_quotas_until_checked(
            org_id="org-1", billed_user_id="user-1", caller_trial_id="trial-1"
        )
        == 0
    )

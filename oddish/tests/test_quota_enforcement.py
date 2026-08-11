from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from oddish.config import NOP_ORACLE_QUEUE_KEY, QuotaMode, settings
from oddish.core import quota_enforcement
from oddish.core.quota_enforcement import (
    QUOTA_CANCELLED_MESSAGE,
    cancel_trials_if_quota_reached,
    enforce_trial_quotas,
    enforce_trial_quotas_until_checked,
)
from oddish.db import (
    AnalysisStatus,
    CostExcludedLlmKeyModel,
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
from oddish.queue import maybe_gate_llm_trials


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
    agent: str = "codex",
    queue_key: str = "openai/gpt-5",
    imported_at: datetime | None = None,
    llm_key_hash: str | None = None,
) -> TrialModel:
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task_id,
        experiment_id=experiment_id,
        org_id=org_id,
        billed_user_id=billed_user_id,
        agent=agent,
        provider="openai",
        queue_key=queue_key,
        model="gpt-5",
        is_probe=False,
        attempts=1,
        max_attempts=6,
        status=status,
        cost_usd=cost_usd,
        finished_at=finished_at,
        imported_at=imported_at,
        llm_key_hash=llm_key_hash,
    )


def _job(
    *,
    subject_table: str,
    subject_id: str,
    kind: WorkerJobKind,
    modal_id: str | None,
    status: WorkerJobStatus = WorkerJobStatus.RUNNING,
) -> WorkerJobModel:
    return WorkerJobModel(
        kind=kind,
        status=status,
        queue_key="openai/gpt-5",
        subject_table=subject_table,
        subject_id=subject_id,
        modal_function_call_id=modal_id,
        provider="daytona" if modal_id else None,
        external_id=f"sandbox-{modal_id}" if modal_id else None,
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
    gate_task_id = f"task-gate-qc-{suffix}"
    now = datetime.now(timezone.utc)
    preserved_verdict = {"verdict": "accept", "is_good": True}

    async def no_org_limit(*_args):
        return None

    async def user_limit(*_args):
        return Decimal("0.30")

    monkeypatch.setattr(quota_enforcement, "get_effective_org_limit", no_org_limit)
    monkeypatch.setattr(quota_enforcement, "get_effective_limit", user_limit)

    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    for task_id in (
        exhausted_task_id,
        mixed_task_id,
        advance_task_id,
        gate_task_id,
    ):
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
                    VerdictStatus.RUNNING
                    if task_id in (exhausted_task_id, mixed_task_id)
                    else None
                ),
                verdict=(preserved_verdict if task_id == exhausted_task_id else None),
                verdict_started_at=(now if task_id == exhausted_task_id else None),
                verdict_finished_at=(now if task_id == exhausted_task_id else None),
                run_analysis=task_id == advance_task_id,
            )
        )

    settled_id = f"{exhausted_task_id}-0"
    exhausted_id = f"{exhausted_task_id}-1"
    mixed_target_id = f"{mixed_task_id}-0"
    mixed_other_id = f"{mixed_task_id}-1"
    advance_target_id = f"{advance_task_id}-0"
    advance_other_id = f"{advance_task_id}-1"
    advance_baseline_id = f"{advance_task_id}-baseline"
    gate_baseline_id = f"{gate_task_id}-baseline"
    gate_other_baseline_id = f"{gate_task_id}-other-baseline"
    gate_blocked_id = f"{gate_task_id}-blocked"
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
                imported_at=now,
            ),
            _trial(
                trial_id=advance_baseline_id,
                task_id=advance_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=user_id,
                status=TrialStatus.RUNNING,
                agent="nop",
                queue_key=NOP_ORACLE_QUEUE_KEY,
            ),
            _trial(
                trial_id=gate_baseline_id,
                task_id=gate_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=user_id,
                status=TrialStatus.RUNNING,
                agent="nop",
                queue_key=NOP_ORACLE_QUEUE_KEY,
            ),
            _trial(
                trial_id=gate_other_baseline_id,
                task_id=gate_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=other_user_id,
                status=TrialStatus.RUNNING,
                agent="oracle",
                queue_key=NOP_ORACLE_QUEUE_KEY,
            ),
            _trial(
                trial_id=gate_blocked_id,
                task_id=gate_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id=other_user_id,
                status=TrialStatus.QUEUED,
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
    gate_blocked_job = _job(
        subject_table="trials",
        subject_id=gate_blocked_id,
        kind=WorkerJobKind.TRIAL,
        modal_id=None,
        status=WorkerJobStatus.BLOCKED,
    )
    session.add_all(
        [
            exhausted_job,
            mixed_target_job,
            mixed_other_job,
            qa_job,
            mixed_qa_job,
            gate_blocked_job,
        ]
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
        gate_blocked_job,
    ):
        await session.refresh(job)

    assert result["scope"] == "user"
    assert result["trials_cancelled"] == 5
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
    exhausted_task = await session.get(TaskModel, exhausted_task_id)
    assert exhausted_task.status == TaskStatus.COMPLETED
    assert exhausted_task.verdict == preserved_verdict
    assert exhausted_task.verdict_status == VerdictStatus.SUCCESS
    assert exhausted_task.verdict_error is None
    assert exhausted_task.verdict_started_at is None
    assert exhausted_task.verdict_finished_at == now
    mixed_task = await session.get(TaskModel, mixed_task_id)
    assert mixed_task.status == TaskStatus.VERDICT_PENDING
    assert mixed_task.verdict_status == VerdictStatus.RUNNING
    advance_task = await session.get(TaskModel, advance_task_id)
    assert advance_task.status == TaskStatus.COMPLETED
    assert advance_task.verdict_status is None
    qa_jobs = (
        await session.scalars(
            select(WorkerJobModel).where(
                WorkerJobModel.subject_table == "tasks",
                WorkerJobModel.subject_id == advance_task_id,
                WorkerJobModel.kind == WorkerJobKind.QA,
            )
        )
    ).all()
    assert qa_jobs == []
    assert (await session.get(TaskModel, gate_task_id)).status == TaskStatus.RUNNING
    assert (await session.get(TrialModel, gate_blocked_id)).status == TrialStatus.QUEUED
    assert gate_blocked_job.status == WorkerJobStatus.BLOCKED

    gate_cancelled_baseline = await session.get(TrialModel, gate_baseline_id)
    assert gate_cancelled_baseline.harbor_stage == "cancelled"
    gate_cancelled_baseline.reward = 0.0
    gate_other_baseline = await session.get(TrialModel, gate_other_baseline_id)
    gate_other_baseline.status = TrialStatus.SUCCESS
    gate_other_baseline.reward = 0.0
    gate_other_baseline.finished_at = now
    await session.flush()
    assert await maybe_gate_llm_trials(session, gate_other_baseline_id)
    await session.refresh(gate_blocked_job)
    assert gate_blocked_job.status == WorkerJobStatus.CANCELLED
    assert (
        await session.get(TrialModel, gate_blocked_id)
    ).status == TrialStatus.SKIPPED
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


def test_overshoot_metric_reports_statuses_ages_and_reserved(caplog, monkeypatch):
    """One quota.overshoot_cancelled line: queued vs running, age spread, USD."""
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.25"))
    now = datetime.now(timezone.utc)
    trials = []
    for index, (status, age_s, cost_usd) in enumerate(
        [
            (TrialStatus.QUEUED, 5, None),
            (TrialStatus.PENDING, 30, None),
            (TrialStatus.RUNNING, 600, 1.75),
        ]
    ):
        trial = _trial(
            trial_id=f"task-om-{index}",
            task_id="task-om",
            experiment_id="exp-om",
            org_id="org-om",
            billed_user_id="user-om",
            status=status,
            cost_usd=cost_usd,
        )
        trial.created_at = now - timedelta(seconds=age_s)
        trials.append(trial)

    with caplog.at_level(logging.INFO, logger="oddish.core.quota_enforcement"):
        quota_enforcement._log_overshoot_cancelled(
            trials, scope="user", org_id="org-om", billed_user_id="user-om", now=now
        )

    assert (
        "metric=quota.overshoot_cancelled scope=user org_id=org-om "
        "billed_user_id=user-om trials=3 queued=2 running=1 "
        "age_min_s=5.0 age_median_s=30.0 age_max_s=600.0 reserved_usd=2.25"
    ) in caplog.text


@pytest.mark.asyncio
async def test_org_quota_cancels_every_users_active_trials(
    session, monkeypatch, caplog
):
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org-org-qc-{suffix}"
    experiment_id = f"exp-org-qc-{suffix}"
    task_id = f"task-org-qc-{suffix}"
    preserved_task_id = f"task-org-preserved-qc-{suffix}"
    excluded_key_hash = ("e" * 56) + suffix
    now = datetime.now(timezone.utc)

    async def org_limit(*_args):
        return Decimal("0.30")

    monkeypatch.setattr(quota_enforcement, "get_effective_org_limit", org_limit)
    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    session.add_all(
        [
            TaskModel(
                id=current_task_id,
                name=current_task_id,
                org_id=org_id,
                user="tester",
                task_path="s3://test-bucket/org-quota-cancel-task",
                status=TaskStatus.RUNNING,
            )
            for current_task_id in (task_id, preserved_task_id)
        ]
    )
    session.add(
        CostExcludedLlmKeyModel(
            key_hash=excluded_key_hash,
            key_hint="excluded",
            label="sponsored",
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
            _trial(
                trial_id=f"{preserved_task_id}-baseline",
                task_id=preserved_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id="user-b",
                status=TrialStatus.RUNNING,
                agent="nop",
                queue_key=NOP_ORACLE_QUEUE_KEY,
            ),
            _trial(
                trial_id=f"{preserved_task_id}-excluded",
                task_id=preserved_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id="user-b",
                status=TrialStatus.QUEUED,
                llm_key_hash=excluded_key_hash,
            ),
            _trial(
                trial_id=f"{preserved_task_id}-cancelled",
                task_id=preserved_task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                billed_user_id="user-b",
                status=TrialStatus.QUEUED,
            ),
        ]
    )
    preserved_job = _job(
        subject_table="trials",
        subject_id=f"{preserved_task_id}-excluded",
        kind=WorkerJobKind.TRIAL,
        modal_id=None,
        status=WorkerJobStatus.BLOCKED,
    )
    cancelled_job = _job(
        subject_table="trials",
        subject_id=f"{preserved_task_id}-cancelled",
        kind=WorkerJobKind.TRIAL,
        modal_id=None,
        status=WorkerJobStatus.BLOCKED,
    )
    session.add_all([preserved_job, cancelled_job])
    await session.flush()

    with caplog.at_level(logging.INFO, logger="oddish.core.quota_enforcement"):
        result = await cancel_trials_if_quota_reached(
            session, org_id=org_id, billed_user_id="user-a"
        )

    assert result["scope"] == "org"
    assert result["trials_cancelled"] == 4
    assert (
        f"metric=quota.overshoot_cancelled scope=org org_id={org_id} "
        "billed_user_id=user-a trials=4 queued=2 running=2 age_min_s="
    ) in caplog.text
    assert (await session.get(TrialModel, f"{task_id}-1")).status == TrialStatus.FAILED
    assert (await session.get(TrialModel, f"{task_id}-2")).status == TrialStatus.FAILED
    assert (await session.get(TaskModel, task_id)).status == TaskStatus.FAILED
    assert (
        await session.get(TrialModel, f"{preserved_task_id}-baseline")
    ).status == TrialStatus.FAILED
    assert (
        await session.get(TrialModel, f"{preserved_task_id}-excluded")
    ).status == TrialStatus.QUEUED
    assert (
        await session.get(TrialModel, f"{preserved_task_id}-cancelled")
    ).status == TrialStatus.FAILED
    assert (
        await session.get(TaskModel, preserved_task_id)
    ).status == TaskStatus.RUNNING
    await session.refresh(preserved_job)
    await session.refresh(cancelled_job)
    assert preserved_job.status == WorkerJobStatus.QUEUED
    assert cancelled_job.status == WorkerJobStatus.CANCELLED
    assert result["released_trial_ids"] == [f"{preserved_task_id}-excluded"]


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
            "released_trial_ids": ["trial-released"],
        }

    termination_calls = []
    check_finished = False
    gate_release_calls = []

    async def after_check():
        nonlocal check_finished
        check_finished = True

    async def after_gate_release(trial_ids):
        gate_release_calls.append(list(trial_ids))

    async def fake_terminate(result, *, strict):
        assert strict
        assert check_finished
        termination_calls.append(dict(result))
        return len(result["modal_function_call_ids"])

    monkeypatch.setattr(quota_enforcement, "get_session", fake_get_session)
    monkeypatch.setattr(
        quota_enforcement, "cancel_trials_if_quota_reached", fake_cancel
    )
    monkeypatch.setattr(quota_enforcement, "terminate_run_harvest", fake_terminate)

    cancelled = await enforce_trial_quotas(
        org_id="org-1",
        billed_user_id="user-1",
        caller_trial_id="trial-1",
        after_check=after_check,
        after_gate_release=after_gate_release,
    )

    assert cancelled == 2
    assert gate_release_calls == [["trial-released"]]
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
            "modal_function_call_ids": ["fc-ok", "fc-sibling"],
            "caller_modal_function_call_ids": ["fc-caller"],
            "worker_targets": [
                ("daytona", "sandbox-ok"),
                ("daytona", "sandbox-sibling"),
            ],
            "released_trial_ids": [],
        }

    termination_calls = []
    sleeps = []
    sibling_failures = 1
    caller_failures = 1

    async def flaky_terminate(result, *, strict):
        nonlocal sibling_failures, caller_failures
        assert strict
        termination_calls.append(
            {
                "modal_function_call_ids": list(result["modal_function_call_ids"]),
                "worker_targets": list(result["worker_targets"]),
            }
        )
        caller = result["modal_function_call_ids"] == ["fc-caller"]
        should_fail = caller_failures if caller else sibling_failures
        if should_fail:
            if caller:
                caller_failures -= 1
                raise quota_enforcement.HarvestTerminationError(["fc-caller"], [])
            else:
                sibling_failures -= 1
                raise quota_enforcement.HarvestTerminationError(
                    ["fc-sibling"], [("daytona", "sandbox-sibling")]
                )

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
        "modal_function_call_ids": ["fc-ok", "fc-sibling"],
        "worker_targets": [
            ("daytona", "sandbox-ok"),
            ("daytona", "sandbox-sibling"),
        ],
    }
    sibling_retry = {
        "modal_function_call_ids": ["fc-sibling"],
        "worker_targets": [("daytona", "sandbox-sibling")],
    }
    caller_harvest = {
        "modal_function_call_ids": ["fc-caller"],
        "worker_targets": [],
    }
    assert termination_calls == [
        sibling_harvest,
        sibling_retry,
        caller_harvest,
        caller_harvest,
    ]
    assert sleeps == [1.0, 1.0]


@pytest.mark.asyncio
async def test_enforcement_tears_down_remotes_when_after_check_fails(monkeypatch):
    @asynccontextmanager
    async def fake_get_session():
        yield object()

    async def fake_cancel(*_args, **_kwargs):
        return {
            "scope": "user",
            "trials_cancelled": 1,
            "tasks_cancelled": 1,
            "modal_function_call_ids": ["fc-sibling"],
            "caller_modal_function_call_ids": ["fc-caller"],
            "worker_targets": [],
            "released_trial_ids": [],
        }

    async def fail_after_check():
        raise RuntimeError("post-trial hook failed")

    termination_calls = []

    async def fake_terminate(result, *, strict):
        assert strict
        termination_calls.append(list(result["modal_function_call_ids"]))

    monkeypatch.setattr(quota_enforcement, "get_session", fake_get_session)
    monkeypatch.setattr(
        quota_enforcement, "cancel_trials_if_quota_reached", fake_cancel
    )
    monkeypatch.setattr(quota_enforcement, "terminate_run_harvest", fake_terminate)

    with pytest.raises(RuntimeError, match="post-trial hook failed"):
        await enforce_trial_quotas(
            org_id="org-1",
            billed_user_id="user-1",
            caller_trial_id="trial-1",
            after_check=fail_after_check,
        )

    assert termination_calls == [["fc-sibling"], ["fc-caller"]]


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
    check_finished = False

    async def unexpected_check(**_kwargs):
        raise AssertionError("quota check should be skipped")

    async def after_check():
        nonlocal check_finished
        check_finished = True

    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    monkeypatch.setattr(quota_enforcement, "enforce_trial_quotas", unexpected_check)

    assert (
        await enforce_trial_quotas_until_checked(
            org_id="org-1",
            billed_user_id="user-1",
            caller_trial_id="trial-1",
            after_check=after_check,
        )
        == 0
    )
    assert check_finished

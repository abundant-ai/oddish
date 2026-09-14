"""PostgreSQL regressions for approval cleanup and org-scoped job diagnostics."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import worker.org_access as access
from models import OrganizationModel
from oddish.core.admin import get_worker_jobs_admin_core
from oddish.db import (
    ExperimentModel,
    JobStatus,
    TaskModel,
    TrialModel,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("ODDISH_DATABASE_URL"), reason="local PostgreSQL required"
    ),
]


@pytest.mark.parametrize("source", ["trial", "task_job", "trial_job"])
@pytest.mark.parametrize("approval", ["approved", "unapproved", "ownerless", "deleted"])
async def test_cleanup_finds_each_active_source_and_preserves_approved(
    monkeypatch,
    source,
    approval,
):
    prefix = "cleanup_" + uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    teardown = AsyncMock()
    monkeypatch.setattr(access, "terminate_run_harvest", teardown)
    try:
        async with get_session() as session:
            session.add(
                OrganizationModel(
                    id=prefix,
                    name=prefix,
                    slug=prefix,
                    execution_enabled=approval in ("approved", "deleted"),
                    deleted_at=now if approval == "deleted" else None,
                )
            )
            session.add(ExperimentModel(id=prefix, name=prefix))
            session.add(
                TaskModel(
                    id=prefix,
                    name=prefix,
                    user="test",
                    task_path="test",
                    org_id=None if approval == "ownerless" else prefix,
                )
            )
            await session.flush()
            session.add(
                TrialModel(
                    id=prefix,
                    name=prefix,
                    task_id=prefix,
                    experiment_id=prefix,
                    agent="nop",
                    provider="test",
                    queue_key="nop_oracle",
                    status=JobStatus.RUNNING
                    if source == "trial"
                    else JobStatus.SUCCESS,
                )
            )
            if source != "trial":
                session.add(
                    WorkerJobModel(
                        id=prefix,
                        kind=WorkerJobKind.VERDICT,
                        subject_table="tasks" if source == "task_job" else "trials",
                        subject_id=prefix,
                        queue_key="default",
                        status=WorkerJobStatus.BLOCKED,
                        org_id=prefix,
                    )
                )
        assert await access.cancel_unapproved_runs() == (
            0 if approval == "approved" else 1
        )
        async with get_session() as session:
            trial = await session.get(TrialModel, prefix)
            if source == "trial":
                assert trial.status == (
                    JobStatus.RUNNING if approval == "approved" else JobStatus.FAILED
                )
            else:
                job = await session.get(WorkerJobModel, prefix)
                assert job.status == (
                    WorkerJobStatus.BLOCKED
                    if approval == "approved"
                    else WorkerJobStatus.CANCELLED
                )
                assert trial.status == JobStatus.SUCCESS
        if approval != "approved":
            teardown.assert_awaited_once()
            assert await access.cancel_unapproved_runs() == 0
        else:
            teardown.assert_not_awaited()
    finally:
        async with get_session() as session:
            for model in (
                WorkerJobModel,
                TrialModel,
                TaskModel,
                ExperimentModel,
                OrganizationModel,
            ):
                await session.execute(
                    model.__table__.delete()
                    .where(model.id == prefix)
                    .execution_options(include_deleted=True)
                )


async def test_admin_samples_counts_and_durations_are_org_scoped():
    prefix = "diag_" + uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    try:
        async with get_session() as session:
            for org in (prefix, prefix + "_foreign"):
                for i, status in enumerate(
                    [
                        WorkerJobStatus.RUNNING,
                        WorkerJobStatus.CANCELLED,
                        WorkerJobStatus.FAILED,
                        WorkerJobStatus.SUCCESS,
                        WorkerJobStatus.SUCCESS,
                    ]
                ):
                    session.add(
                        WorkerJobModel(
                            id=f"{org}_{i}",
                            org_id=org,
                            kind=WorkerJobKind.TRIAL,
                            subject_table="trials",
                            subject_id="fixture",
                            queue_key="default",
                            status=status,
                            claimed_at=now - timedelta(minutes=25),
                            heartbeat_at=now - timedelta(minutes=20),
                            finished_at=None
                            if status == WorkerJobStatus.RUNNING
                            else now - timedelta(minutes=5),
                        )
                    )
        async with get_session() as session:
            result = await get_worker_jobs_admin_core(session, org_id=prefix)
        assert result.counts == {
            "TRIAL": {"RUNNING": 1, "CANCELLED": 1, "FAILED": 1, "SUCCESS": 2}
        }
        assert [r.id for r in result.stale_running] == [prefix + "_0"]
        assert {r.id for r in result.recent_failures} == {prefix + "_1", prefix + "_2"}
        assert len(result.durations_last_hour) == 1
        assert result.durations_last_hour[0].sample_count == 3
        assert result.durations_last_hour[0].p50_seconds == 1200
    finally:
        async with get_session() as session:
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.id.like(prefix + "%")
                )
            )

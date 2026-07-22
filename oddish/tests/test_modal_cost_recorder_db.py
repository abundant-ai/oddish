from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from oddish.costs.modal_cost import SpanResources
from oddish.costs.recorder import (
    WorkerBillingSpec,
    close_agent_sandboxes,
    close_worker_span,
    open_worker_span,
    reconcile_compute_cost_spans,
    transition_agent_sandbox,
)
from oddish.db import (
    ExperimentModel,
    ModalCostSpanModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
)

requires_db = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"), reason="ODDISH_DATABASE_URL not set"
)


def _sandbox_resources() -> SpanResources:
    return SpanResources(
        cpu_request=2,
        cpu_limit=2,
        mem_request_mb=4096,
        mem_limit_mb=None,
        gpu_type=None,
        gpu_count=0,
        price_multiplier=Decimal(1),
        container_class="sandbox",
        spec_source="pinned",
        cpu_enforcement_mode="auto",
        mem_enforcement_mode="auto",
    )


async def _seed() -> tuple[str, str, str, str]:
    suffix = uuid.uuid4().hex[:10]
    experiment_id = f"modal-exp-{suffix}"
    task_id = f"modal-task-{suffix}"
    trial_id = f"modal-trial-{suffix}"
    worker_job_id = f"modal-job-{suffix}"
    async with get_session() as session:
        session.add(
            ExperimentModel(id=experiment_id, name=experiment_id, org_id="org-modal")
        )
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                org_id="org-modal",
                user="tester",
                task_path="/tmp/modal-cost-test",
                status=TaskStatus.RUNNING,
            )
        )
        session.add(
            TrialModel(
                id=trial_id,
                name=trial_id,
                task_id=task_id,
                experiment_id=experiment_id,
                org_id="org-modal",
                billed_user_id="user-modal",
                agent="codex",
                provider="openai",
                queue_key="openai/gpt-5",
                model="gpt-5",
                is_probe=False,
                max_attempts=6,
                attempts=2,
                status=TrialStatus.RUNNING,
            )
        )
        session.add(
            WorkerJobModel(
                id=worker_job_id,
                kind=WorkerJobKind.TRIAL,
                status=WorkerJobStatus.RUNNING,
                queue_key="openai/gpt-5",
                subject_table="trials",
                subject_id=trial_id,
                attempts=3,
                current_worker_id="worker-modal",
            )
        )
    return experiment_id, task_id, trial_id, worker_job_id


async def _remove(ids: tuple[str, str, str, str]) -> None:
    experiment_id, task_id, trial_id, worker_job_id = ids
    async with get_session() as session:
        await session.execute(
            ModalCostSpanModel.__table__.delete().where(
                ModalCostSpanModel.worker_job_id == worker_job_id
            )
        )
        await session.execute(
            WorkerJobModel.__table__.delete().where(WorkerJobModel.id == worker_job_id)
        )
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.id == trial_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )


@requires_db
@pytest.mark.asyncio
async def test_worker_and_internal_retry_spans_are_idempotent_and_priced() -> None:
    ids = await _seed()
    experiment_id, _, trial_id, worker_job_id = ids
    t0 = datetime(2026, 7, 22, tzinfo=timezone.utc)
    job = SimpleNamespace(
        id=worker_job_id,
        attempts=3,
        subject_table="trials",
        subject_id=trial_id,
    )
    try:
        spec = WorkerBillingSpec(cpu_cores=1, memory_mb=3072, nonpreemptible=True)
        await open_worker_span(job, spec, started_at=t0)
        await open_worker_span(job, spec, started_at=t0 + timedelta(seconds=1))
        await close_worker_span(
            worker_job_id, 3, finished_at=t0 + timedelta(seconds=60)
        )

        common = dict(
            worker_job_id=worker_job_id,
            worker_job_attempt=3,
            trial_id=trial_id,
            attempt=2,
            experiment_id=experiment_id,
            org_id="org-modal",
            billed_user_id="user-modal",
            provider="modal",
            resources=_sandbox_resources(),
        )
        await transition_agent_sandbox(
            **common, external_id=f"sb-{worker_job_id}-1", observed_at=t0
        )
        await transition_agent_sandbox(
            **common,
            external_id=f"sb-{worker_job_id}-2",
            observed_at=t0 + timedelta(seconds=20),
        )
        await close_agent_sandboxes(
            worker_job_id, 3, finished_at=t0 + timedelta(seconds=50)
        )

        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(ModalCostSpanModel)
                        .where(ModalCostSpanModel.worker_job_id == worker_job_id)
                        .order_by(
                            ModalCostSpanModel.component_role,
                            ModalCostSpanModel.span_ordinal,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [row.component_role for row in rows] == [
            "agent_sandbox",
            "agent_sandbox",
            "worker_function",
        ]
        assert rows[0].finished_at == t0 + timedelta(seconds=20)
        assert rows[1].finished_at == t0 + timedelta(seconds=50)
        assert rows[0].span_ordinal == 0
        assert rows[1].span_ordinal == 1
        assert all(row.cost_usd is not None and row.cost_usd > 0 for row in rows)
        assert rows[2].price_multiplier == Decimal(3)
        assert rows[2].trial_id == trial_id
    finally:
        await _remove(ids)


@requires_db
@pytest.mark.asyncio
async def test_terminal_reconciliation_uses_trial_heartbeat_fallback() -> None:
    ids = await _seed()
    experiment_id, _, trial_id, worker_job_id = ids
    t0 = datetime(2026, 7, 22, tzinfo=timezone.utc)
    heartbeat = t0 + timedelta(seconds=41)
    try:
        await transition_agent_sandbox(
            worker_job_id=worker_job_id,
            worker_job_attempt=3,
            trial_id=trial_id,
            attempt=2,
            experiment_id=experiment_id,
            org_id="org-modal",
            billed_user_id="user-modal",
            provider="modal",
            external_id=f"sb-{worker_job_id}",
            resources=_sandbox_resources(),
            observed_at=t0,
        )
        async with get_session() as session:
            await session.execute(
                update(WorkerJobModel)
                .where(WorkerJobModel.id == worker_job_id)
                .values(status=WorkerJobStatus.CANCELLED, finished_at=None)
            )
            await session.execute(
                update(TrialModel)
                .where(TrialModel.id == trial_id)
                .values(heartbeat_at=heartbeat)
            )

        assert await reconcile_compute_cost_spans() == 1
        assert await reconcile_compute_cost_spans() == 0
        async with get_session() as session:
            row = await session.scalar(
                select(ModalCostSpanModel).where(
                    ModalCostSpanModel.worker_job_id == worker_job_id
                )
            )
        assert row is not None
        assert row.finished_at == heartbeat
        assert row.basis == "reconciled"
        assert row.cost_usd is not None and row.cost_usd > 0
    finally:
        await _remove(ids)


@requires_db
@pytest.mark.asyncio
async def test_reconcile_does_not_bill_stale_attempt_through_later_finished_at() -> (
    None
):
    # A span left open by attempt 1 must not be closed at the job's terminal
    # finished_at (which belongs to a later attempt). It closes at its own
    # started_at instead -- a ~zero-duration floor, never a later attempt's run.
    ids = await _seed()
    experiment_id, _, trial_id, worker_job_id = ids
    t0 = datetime(2026, 7, 22, tzinfo=timezone.utc)
    job_finished = t0 + timedelta(hours=1)  # end of a LATER attempt
    try:
        await transition_agent_sandbox(
            worker_job_id=worker_job_id,
            worker_job_attempt=1,  # stale: the job is on attempt 3 (see _seed)
            trial_id=trial_id,
            attempt=1,
            experiment_id=experiment_id,
            org_id="org-modal",
            billed_user_id="user-modal",
            provider="modal",
            external_id=f"sb-stale-{worker_job_id}",
            resources=_sandbox_resources(),
            observed_at=t0,
        )
        async with get_session() as session:
            await session.execute(
                update(WorkerJobModel)
                .where(WorkerJobModel.id == worker_job_id)
                .values(status=WorkerJobStatus.FAILED, finished_at=job_finished)
            )

        assert await reconcile_compute_cost_spans() == 1
        async with get_session() as session:
            row = await session.scalar(
                select(ModalCostSpanModel).where(
                    ModalCostSpanModel.worker_job_id == worker_job_id
                )
            )
        assert row is not None
        assert row.finished_at == t0  # its own start, NOT job_finished (t0 + 1h)
        assert row.cost_usd == Decimal("0.000000")  # zero duration -> zero cost
        assert row.basis == "reconciled"
    finally:
        await _remove(ids)

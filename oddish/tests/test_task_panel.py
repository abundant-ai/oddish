from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from oddish.core.endpoints.task_panel import get_task_panel_core
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    WorkerJobModel,
    WorkerJobKind,
    WorkerJobStatus,
    utcnow,
)


async def seed(session):
    task = TaskModel(
        name=f"panel-{uuid4().hex}",
        org_id="panel-org",
        user="tester",
        task_path="s3://task",
    )
    session.add(task)
    await session.flush()
    versions = [
        TaskVersionModel(
            id=f"{task.id}-v{n}",
            task_id=task.id,
            version=n,
            task_path=task.task_path,
            content_hash=f"hash-{n}",
            pre_trial={"items": [{"title": f"finding-{n}"}]},
            pre_trial_status="SUCCESS",
        )
        for n in (1, 2)
    ]
    session.add_all(versions)
    await session.flush()
    task.current_version_id = versions[0].id
    experiment = ExperimentModel(name=f"panel-exp-{uuid4().hex}", org_id=task.org_id)
    session.add(experiment)
    await session.flush()
    trial = TrialModel(
        id=uuid4().hex,
        name="solver",
        task_id=task.id,
        task_version_id=versions[0].id,
        experiment_id=experiment.id,
        org_id=task.org_id,
        agent="codex",
        provider="openai",
        queue_key="test",
        status=TrialStatus.SUCCESS,
        reward=1,
    )
    session.add(trial)
    await session.flush()
    return task, versions, trial


@pytest.mark.asyncio
async def test_panel_two_queries_versions_and_authorization(session):
    task, versions, _ = await seed(session)
    queries = []
    engine = session.bind.sync_engine

    def record(_conn, _cursor, statement, *_):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        panel = await get_task_panel_core(session, task_id=task.id, org_id=task.org_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(queries) == 2
    assert panel.version.version == 1  # selected default, not numerically latest
    assert panel.version.pre_trial_findings == [{"title": "finding-1"}]
    assert panel.task.trials is None
    assert panel.task.reward_total == 1
    assert panel.can_retry and panel.can_run_qa and panel.cancel is None
    pinned = await get_task_panel_core(
        session, task_id=task.id, version=2, org_id=task.org_id
    )
    assert pinned.version.content_hash == "hash-2"
    assert pinned.task.current_version == 1
    for args in ({"org_id": "another-org"}, {"version": 99}):
        with pytest.raises(HTTPException) as exc:
            await get_task_panel_core(session, task_id=task.id, **args)
        assert exc.value.status_code == 404
    versions[1].deleted_at = utcnow()
    await session.flush()
    with pytest.raises(HTTPException):
        await get_task_panel_core(session, task_id=task.id, version=2)
    task.deleted_at = utcnow()
    await session.flush()
    with pytest.raises(HTTPException):
        await get_task_panel_core(session, task_id=task.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,status,cancel,can_retry,can_qa,qa_active",
    [
        ("agent", "RUNNING", "task", False, False, False),
        ("agent", "FAILED", None, True, True, False),
        ("agent", "SKIPPED", None, False, True, False),
        ("qa", "QUEUED", "qa", False, False, True),
        ("audit", "RUNNING", "qa", False, False, False),
    ],
)
async def test_panel_actions(
    session, kind, status, cancel, can_retry, can_qa, qa_active
):
    task, _, trial = await seed(session)
    trial.kind, trial.status = kind, TrialStatus[status]
    await session.flush()
    panel = await get_task_panel_core(session, task_id=task.id)
    assert (panel.cancel, panel.can_retry, panel.can_run_qa, panel.qa_active) == (
        cancel,
        can_retry,
        can_qa,
        qa_active,
    )
    trial.superseded_by_trial_id = trial.id
    await session.flush()
    empty = await get_task_panel_core(session, task_id=task.id)
    assert not empty.can_retry and not empty.can_run_qa and empty.cancel is None


@pytest.mark.asyncio
async def test_panel_jobs_and_missing_audit(session):
    task, versions, trial = await seed(session)
    versions[0].pre_trial = versions[0].pre_trial_status = None
    job = WorkerJobModel(
        kind=WorkerJobKind.ANALYSIS,
        status=WorkerJobStatus.BLOCKED,
        subject_table="trials",
        subject_id=trial.id,
        queue_key="test",
    )
    session.add(job)
    await session.flush()
    panel = await get_task_panel_core(session, task_id=task.id)
    assert panel.cancel == "qa"
    assert panel.version.pre_trial_status is None
    assert panel.version.pre_trial_findings == []
    job.kind, job.subject_table, job.subject_id = WorkerJobKind.QA, "tasks", task.id
    await session.flush()
    panel = await get_task_panel_core(session, task_id=task.id)
    assert panel.qa_active and not panel.can_run_qa


@pytest.mark.asyncio
async def test_panel_qa_worker_is_not_a_solver_and_deleted_jobs_do_not_block(session):
    task, _, trial = await seed(session)
    trial.kind, trial.status = "qa", TrialStatus.RUNNING
    job = WorkerJobModel(
        kind=WorkerJobKind.TRIAL,
        status=WorkerJobStatus.RUNNING,
        subject_table="trials",
        subject_id=trial.id,
        queue_key="test",
    )
    session.add(job)
    await session.flush()
    panel = await get_task_panel_core(session, task_id=task.id)
    assert panel.cancel == "qa" and panel.active_trials == 0
    trial.status = TrialStatus.SUCCESS
    job.deleted_at = utcnow()
    await session.flush()
    panel = await get_task_panel_core(session, task_id=task.id)
    assert panel.cancel is None and panel.can_run_qa

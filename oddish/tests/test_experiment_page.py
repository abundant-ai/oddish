from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from oddish.core.endpoints.experiment_page import (
    OPEN_MAX_BYTES,
    OPEN_MAX_TASKS,
    TRIAL_PAGE_MAX_TRIALS,
    ExperimentReadScope,
    _trial_projection,
    read_experiment_open,
    read_experiment_revision,
    read_experiment_trial_page,
    resolve_member_experiment_scope,
    resolve_public_experiment_scope,
)
from oddish.core.helpers import experiment_effective_versions_selectable
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    experiment_trials,
    task_experiments,
)
from oddish.timing import begin_request_timing, reset_request_timing


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def test_trial_projection_selects_only_grid_fields():
    scope = ExperimentReadScope(
        experiment_id="experiment-1",
        org_id="org-1",
        name="Projected",
        created_at=datetime.now(UTC),
        owner=None,
        link=None,
        revision=datetime.now(UTC),
        audience="member",
        model_display_names={},
    )
    versions = experiment_effective_versions_selectable(experiment_id=scope.experiment_id)
    sql = str(
        _trial_projection(scope, versions).compile(dialect=postgresql.dialect())
    )
    assert "trials.analysis ->>" in sql
    for wide_column in (
        "trials.phase_timing",
        "trials.result",
        "trials.harbor_config",
        "trials.error_message",
    ):
        assert wide_column not in sql


async def _add_task(
    session,
    experiment: ExperimentModel,
    *,
    name: str,
    task_path: str | None = None,
) -> tuple[TaskModel, TaskVersionModel]:
    task = TaskModel(
        id=_id("task"),
        name=name,
        org_id=experiment.org_id,
        user="tester",
        task_path=task_path or f"s3://tasks/{name}",
    )
    session.add(task)
    await session.flush()
    version = TaskVersionModel(
        id=f"{task.id}-v1",
        task_id=task.id,
        version=1,
        task_path=task.task_path,
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    await session.execute(
        task_experiments.insert().values(
            task_id=task.id,
            experiment_id=experiment.id,
        )
    )
    return task, version


def _trial(
    experiment: ExperimentModel,
    task: TaskModel,
    version: TaskVersionModel | None,
    *,
    name: str,
    status: TrialStatus = TrialStatus.SUCCESS,
    kind: str = "agent",
    reward: float | None = 1.0,
    home_experiment_id: str | None = None,
) -> TrialModel:
    return TrialModel(
        id=_id("trial"),
        name=name,
        task_id=task.id,
        task_version_id=version.id if version else None,
        experiment_id=home_experiment_id or experiment.id,
        org_id=experiment.org_id,
        agent="codex",
        provider="openai",
        queue_key="openai/openai/gpt-5.6",
        model="openai/gpt-5.6",
        status=status,
        kind=kind,
        reward=reward,
        input_tokens=100,
        output_tokens=20,
        analysis={
            "classification": "GOOD_SUCCESS",
            "subtype": "correct",
            "private_body": "a" * 5_000,
        },
        phase_timing={"private_body": "p" * 5_000},
        result={"private_body": "r" * 5_000},
        harbor_config={"private_body": "h" * 5_000},
        error_message="e" * 5_000,
    )


@pytest.mark.asyncio
async def test_bounded_pages_are_exact_flat_and_publicly_redacted(session):
    experiment = ExperimentModel(
        id=_id("experiment"),
        name="Bounded experiment",
        org_id="org-bounded",
        is_public=True,
        public_token=_id("token"),
        public_model_renames={"openai/gpt-5.6": "Model A"},
    )
    private_home = ExperimentModel(
        id=_id("private"), name="Private home", org_id=experiment.org_id
    )
    session.add_all([experiment, private_home])
    await session.flush()

    trial_ids: set[str] = set()
    gathered_id: str | None = None
    for task_index in range(100):
        task, version = await _add_task(
            session, experiment, name=f"task-{task_index:03}"
        )
        for trial_index in range(5):
            gathered = task_index == 0 and trial_index == 0
            trial = _trial(
                experiment,
                task,
                version,
                name=f"trial-{task_index}-{trial_index}",
                home_experiment_id=private_home.id if gathered else None,
            )
            session.add(trial)
            trial_ids.add(trial.id)
            if gathered:
                gathered_id = trial.id
    await session.flush()
    assert gathered_id is not None
    await session.execute(
        experiment_trials.insert().values(
            experiment_id=experiment.id,
            trial_id=gathered_id,
        )
    )
    await session.flush()

    member_scope = await resolve_member_experiment_scope(
        session, experiment_id=experiment.id, org_id=experiment.org_id
    )
    open_timing, *open_tokens = begin_request_timing()
    try:
        opened = await read_experiment_open(session, scope=member_scope)
    finally:
        reset_request_timing(tuple(open_tokens))
    assert opened.summary.task_count == 100
    assert opened.summary.trial_count == 500
    assert opened.summary.completed == 500
    assert len(opened.tasks) <= OPEN_MAX_TASKS
    assert len(opened.model_dump_json(exclude_none=True).encode()) < OPEN_MAX_BYTES
    assert open_timing.handler_query_count <= 2

    trial_timing, *trial_tokens = begin_request_timing()
    try:
        first = await read_experiment_trial_page(session, scope=member_scope)
    finally:
        reset_request_timing(tuple(trial_tokens))
    assert trial_timing.handler_query_count <= 5
    second = await read_experiment_trial_page(
        session, scope=member_scope, cursor=first.next_cursor
    )
    assert len(first.trials) == TRIAL_PAGE_MAX_TRIALS
    assert len(second.trials) == TRIAL_PAGE_MAX_TRIALS
    assert {trial.id for trial in [*first.trials, *second.trials]} == trial_ids
    member_json = first.model_dump_json(exclude_none=True)
    for private_field in (
        "private_body",
        "phase_timing",
        "result",
        "harbor_config",
        "error_message",
    ):
        assert private_field not in member_json

    public_scope = await resolve_public_experiment_scope(
        session, public_token=experiment.public_token
    )
    public_open = await read_experiment_open(session, scope=public_scope)
    public_page = await read_experiment_trial_page(session, scope=public_scope)
    assert [task.id for task in public_open.tasks] == [task.id for task in opened.tasks]
    assert [trial.id for trial in public_page.trials] == [
        trial.id for trial in first.trials
    ]
    public_json = public_page.model_dump_json(exclude_none=True)
    assert {trial.model for trial in public_page.trials} == {"Model A"}
    assert "openai/gpt-5.6" not in public_json
    assert private_home.id not in public_json
    assert "owned_here" not in public_json
    assert "is_billed" not in public_json
    assert "cost_exclusion_reason" not in public_json


@pytest.mark.asyncio
async def test_displayed_version_prefers_real_version_over_legacy_null(session):
    experiment = ExperimentModel(
        id=_id("experiment"), name="Versions", org_id="org-versions"
    )
    session.add(experiment)
    await session.flush()
    task, represented = await _add_task(session, experiment, name="versioned")
    current = TaskVersionModel(
        id=f"{task.id}-v2",
        task_id=task.id,
        version=2,
        task_path=task.task_path,
    )
    session.add(current)
    await session.flush()
    task.current_version_id = current.id
    real = _trial(experiment, task, represented, name="real")
    legacy = _trial(
        experiment,
        task,
        None,
        name="legacy",
        status=TrialStatus.FAILED,
        reward=None,
    )
    session.add_all([real, legacy])
    await session.flush()

    scope = await resolve_member_experiment_scope(
        session, experiment_id=experiment.id, org_id=experiment.org_id
    )
    opened = await read_experiment_open(session, scope=scope)
    page = await read_experiment_trial_page(session, scope=scope)
    assert opened.tasks[0].current_version_id == current.id
    assert opened.tasks[0].trial_version_id == represented.id
    assert opened.summary.trial_count == 1
    assert [trial.id for trial in page.trials] == [real.id]


@pytest.mark.asyncio
async def test_analysis_work_uses_the_same_activity_rule_for_open_and_revision(session):
    experiment = ExperimentModel(
        id=_id("experiment"), name="Activity", org_id="org-activity"
    )
    session.add(experiment)
    await session.flush()
    task, version = await _add_task(session, experiment, name="activity")
    session.add_all(
        [
            _trial(experiment, task, version, name="agent"),
            _trial(
                experiment,
                task,
                version,
                name="qa",
                status=TrialStatus.QUEUED,
                kind="qa",
                reward=None,
            ),
        ]
    )
    await session.flush()

    scope = await resolve_member_experiment_scope(
        session, experiment_id=experiment.id, org_id=experiment.org_id
    )
    opened = await read_experiment_open(session, scope=scope)
    revision = await read_experiment_revision(session, scope=scope)
    assert opened.has_active_trials is True
    assert revision.has_active_trials is True
    assert opened.summary.active == 0


@pytest.mark.asyncio
async def test_open_rejects_a_single_task_over_the_byte_contract(session):
    experiment = ExperimentModel(
        id=_id("experiment"), name="Oversized", org_id="org-oversized"
    )
    session.add(experiment)
    await session.flush()
    await _add_task(
        session,
        experiment,
        name="oversized",
        task_path=f"s3://tasks/{'x' * OPEN_MAX_BYTES}",
    )
    await session.flush()
    scope = await resolve_member_experiment_scope(
        session, experiment_id=experiment.id, org_id=experiment.org_id
    )
    with pytest.raises(HTTPException, match="One task row exceeds"):
        await read_experiment_open(session, scope=scope)

from __future__ import annotations

import uuid

import pytest

from oddish.core.endpoints.experiment_open import (
    OPEN_MAX_BYTES,
    TRIAL_PAGE_MAX_BYTES,
    TRIAL_PAGE_MAX_TRIALS,
    get_experiment_open,
    get_experiment_trial_page,
    resolve_member_experiment_read_scope,
    resolve_public_experiment_read_scope,
)
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    task_experiments,
)
from oddish.timing import begin_request_timing, reset_request_timing


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.mark.asyncio
async def test_experiment_open_is_bounded_projected_and_shared_by_audience(session):
    experiment = ExperimentModel(
        id=_id("experiment"),
        name="Bounded experiment",
        org_id="org-bounded",
        is_public=True,
        public_token=_id("share"),
        public_model_renames={"openai/gpt-5.6": "Model A"},
        description="Experiment description",
    )
    session.add(experiment)
    await session.flush()

    task_ids: list[str] = []
    trial_ids: list[str] = []
    for task_index in range(100):
        task = TaskModel(
            id=_id(f"task-{task_index}"),
            name=f"task-{task_index:03}",
            org_id=experiment.org_id,
            user="tester",
            task_path=f"s3://tasks/{task_index}",
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
                task_id=task.id, experiment_id=experiment.id
            )
        )
        for trial_index in range(5):
            trial_id = _id(f"trial-{task_index}-{trial_index}")
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task.id,
                    task_version_id=version.id,
                    experiment_id=experiment.id,
                    org_id=experiment.org_id,
                    agent="codex",
                    provider="openai",
                    queue_key="openai/openai/gpt-5.6",
                    model="openai/gpt-5.6",
                    status=TrialStatus.SUCCESS,
                    reward=1.0,
                    input_tokens=100,
                    output_tokens=20,
                    has_trajectory=True,
                    # These payloads prove the bounded readers do not serialize
                    # detail-only trial columns even when the database rows have them.
                    analysis={"classification": "GOOD_SUCCESS", "body": "a" * 20_000},
                    phase_timing={"body": "p" * 20_000},
                    result={"body": "r" * 20_000},
                    harbor_config={"body": "h" * 20_000},
                    error_message="e" * 20_000,
                )
            )
            trial_ids.append(trial_id)
        task_ids.append(task.id)
    await session.flush()

    timing, *tokens = begin_request_timing()
    try:
        member_scope = await resolve_member_experiment_read_scope(
            session, experiment_id=experiment.id, org_id="org-bounded"
        )
        opened = await get_experiment_open(session, scope=member_scope)
    finally:
        reset_request_timing(tuple(tokens))

    opened_json = opened.model_dump_json(exclude_none=True)
    assert len(opened_json.encode("utf-8")) <= OPEN_MAX_BYTES
    assert timing.handler_query_count <= 5
    assert opened.summary.task_count == 100
    assert opened.summary.trial_count == 500
    assert opened.summary.success_count == 500
    assert opened.summary.active_count == 0
    assert opened.has_active_trials is False
    assert 0 < len(opened.tasks) <= 100
    assert opened.next_cursor is not None
    assert {task.id for task in opened.tasks}.issubset(set(task_ids))
    assert "\"analysis\":" not in opened_json
    assert "\"phase_timing\":" not in opened_json
    assert "\"harbor_config\":" not in opened_json
    assert "\"error_message\":" not in opened_json

    opened_page_2 = await get_experiment_open(
        session, scope=member_scope, cursor=opened.next_cursor
    )
    assert opened_page_2.summary == opened.summary

    member_page = await get_experiment_trial_page(session, scope=member_scope)
    member_json = member_page.model_dump_json(exclude_none=True)
    assert member_page.trial_count == TRIAL_PAGE_MAX_TRIALS
    assert member_page.next_cursor is not None
    assert member_page.trial_count <= TRIAL_PAGE_MAX_TRIALS
    assert len(member_json.encode("utf-8")) <= TRIAL_PAGE_MAX_BYTES
    assert "\"analysis\":" not in member_json
    assert "\"phase_timing\":" not in member_json
    assert "\"result\"" not in member_json
    assert "\"harbor_config\":" not in member_json
    assert "\"error_message\":" not in member_json

    member_page_2 = await get_experiment_trial_page(
        session, scope=member_scope, cursor=member_page.next_cursor
    )
    assert member_page_2.trial_count == 250
    assert member_page_2.next_cursor is None
    assert {
        trial.id
        for page in (member_page, member_page_2)
        for task in page.tasks
        for trial in task.trials
    } == set(trial_ids)

    public_scope = await resolve_public_experiment_read_scope(
        session, public_token=experiment.public_token
    )
    public_open = await get_experiment_open(session, scope=public_scope)
    public_page = await get_experiment_trial_page(session, scope=public_scope)
    public_page_2 = await get_experiment_trial_page(
        session, scope=public_scope, cursor=public_page.next_cursor
    )

    assert [task.id for task in public_open.tasks] == [
        task.id for task in opened.tasks
    ]
    assert [
        trial.id for task in public_page.tasks for trial in task.trials
    ] == [trial.id for task in member_page.tasks for trial in task.trials]
    assert [
        trial.id for task in public_page_2.tasks for trial in task.trials
    ] == [trial.id for task in member_page_2.tasks for trial in task.trials]
    assert {
        trial.model for task in public_page.tasks for trial in task.trials
    } == {"Model A"}
    assert "openai/gpt-5.6" not in public_page.model_dump_json(exclude_none=True)
    assert all(
        trial.is_billed is False
        and trial.cost_exclusion_reason is None
        for task in public_page.tasks
        for trial in task.trials
    )

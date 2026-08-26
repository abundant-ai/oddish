from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from oddish.core.endpoints.experiment_open import (
    OPEN_MAX_BYTES,
    TRIAL_PAGE_MAX_BYTES,
    TRIAL_PAGE_MAX_TRIALS,
    get_experiment_open,
    get_experiment_revision,
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
    VerdictStatus,
    experiment_trials,
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
    private_home = ExperimentModel(
        id=_id("private-home"),
        name="Private home experiment",
        org_id=experiment.org_id,
    )
    session.add_all([experiment, private_home])
    await session.flush()

    task_ids: list[str] = []
    trial_ids: list[str] = []
    gathered_trial_id: str | None = None
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
            trial_experiment_id = (
                private_home.id
                if task_index == 0 and trial_index == 0
                else experiment.id
            )
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task.id,
                    task_version_id=version.id,
                    experiment_id=trial_experiment_id,
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
                    analysis={
                        "classification": "GOOD_SUCCESS",
                        "subtype": "correct",
                        "body": "a" * 20_000,
                    },
                    phase_timing={"body": "p" * 20_000},
                    result={"body": "r" * 20_000},
                    harbor_config={"body": "h" * 20_000},
                    error_message="e" * 20_000,
                )
            )
            if trial_experiment_id == private_home.id:
                gathered_trial_id = trial_id
            trial_ids.append(trial_id)
        task_ids.append(task.id)
    await session.flush()
    assert gathered_trial_id is not None
    await session.execute(
        experiment_trials.insert().values(
            experiment_id=experiment.id,
            trial_id=gathered_trial_id,
        )
    )
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
    assert opened.summary.pass_count == 500
    assert opened.summary.partial_count == 0
    assert opened.summary.fail_count == 0
    assert opened.summary.harness_error_count == 0
    assert opened.summary.avg_score == 1.0
    assert opened.has_active_trials is False
    revision = await get_experiment_revision(session, scope=member_scope)
    assert revision.has_active_trials is False
    assert 0 < len(opened.tasks) <= 100
    assert opened.next_cursor is not None
    assert {task.id for task in opened.tasks}.issubset(set(task_ids))
    assert '"analysis":' not in opened_json
    assert '"phase_timing":' not in opened_json
    assert '"harbor_config":' not in opened_json
    assert '"error_message":' not in opened_json

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
    assert '"analysis":' not in member_json
    assert '"phase_timing":' not in member_json
    assert '"result"' not in member_json
    assert '"harbor_config":' not in member_json
    assert '"error_message":' not in member_json

    member_page_2 = await get_experiment_trial_page(
        session, scope=member_scope, cursor=member_page.next_cursor
    )
    assert member_page_2.trial_count == 250
    assert member_page_2.next_cursor is None
    member_trials = [
        trial
        for page in (member_page, member_page_2)
        for task in page.tasks
        for trial in task.trials
    ]
    assert {trial.id for trial in member_trials} == set(trial_ids)
    assert {trial.experiment_id for trial in member_trials} == {
        experiment.id,
        private_home.id,
    }
    assert all(
        trial.analysis_classification == "GOOD_SUCCESS"
        and trial.analysis_subtype == "correct"
        for trial in member_trials
    )

    public_scope = await resolve_public_experiment_read_scope(
        session, public_token=experiment.public_token
    )
    public_open = await get_experiment_open(session, scope=public_scope)
    public_page = await get_experiment_trial_page(session, scope=public_scope)
    public_page_2 = await get_experiment_trial_page(
        session, scope=public_scope, cursor=public_page.next_cursor
    )

    assert [task.id for task in public_open.tasks] == [task.id for task in opened.tasks]
    assert [trial.id for task in public_page.tasks for trial in task.trials] == [
        trial.id for task in member_page.tasks for trial in task.trials
    ]
    assert [trial.id for task in public_page_2.tasks for trial in task.trials] == [
        trial.id for task in member_page_2.tasks for trial in task.trials
    ]
    assert {
        trial.experiment_id
        for page in (public_page, public_page_2)
        for task in page.tasks
        for trial in task.trials
    } == {experiment.id}
    assert private_home.id not in public_page.model_dump_json(exclude_none=True)
    assert private_home.id not in public_page_2.model_dump_json(exclude_none=True)
    assert {trial.model for task in public_page.tasks for trial in task.trials} == {
        "Model A"
    }
    assert "openai/gpt-5.6" not in public_page.model_dump_json(exclude_none=True)
    assert all(
        trial.is_billed is False and trial.cost_exclusion_reason is None
        for task in public_page.tasks
        for trial in task.trials
    )


@pytest.mark.asyncio
async def test_experiment_activity_includes_analysis_trials_in_open_and_revision(
    session,
):
    experiment = ExperimentModel(
        id=_id("activity-experiment"),
        name="Analysis activity",
        org_id="org-activity",
    )
    task = TaskModel(
        id=_id("activity-task"),
        name="Activity task",
        org_id=experiment.org_id,
        user="tester",
        task_path="s3://tasks/activity",
    )
    session.add_all([experiment, task])
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
    qa_trial = TrialModel(
        id=_id("queued-qa"),
        name="Queued QA trial",
        task_id=task.id,
        task_version_id=version.id,
        experiment_id=experiment.id,
        org_id=experiment.org_id,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.6",
        model="openai/gpt-5.6",
        kind="qa",
        status=TrialStatus.QUEUED,
    )
    session.add_all(
        [
            TrialModel(
                id=_id("settled-agent"),
                name="Settled agent trial",
                task_id=task.id,
                task_version_id=version.id,
                experiment_id=experiment.id,
                org_id=experiment.org_id,
                agent="codex",
                provider="openai",
                queue_key="openai/gpt-5.6",
                model="openai/gpt-5.6",
                kind="agent",
                status=TrialStatus.SUCCESS,
                reward=1.0,
            ),
            qa_trial,
        ]
    )
    await session.flush()

    scope = await resolve_member_experiment_read_scope(
        session,
        experiment_id=experiment.id,
        org_id=experiment.org_id,
    )
    opened = await get_experiment_open(session, scope=scope)
    revision = await get_experiment_revision(session, scope=scope)

    assert opened.summary.active_count == 0
    assert opened.has_active_trials is True
    assert revision.has_active_trials is True

    qa_trial.status = TrialStatus.SUCCESS
    await session.flush()

    settled_open = await get_experiment_open(session, scope=scope)
    settled_revision = await get_experiment_revision(session, scope=scope)
    assert settled_open.revision == opened.revision
    assert settled_open.has_active_trials is False
    assert settled_revision.has_active_trials is False


@pytest.mark.asyncio
async def test_experiment_open_rejects_one_task_shell_over_the_byte_limit(session):
    experiment = ExperimentModel(
        id=_id("oversized-experiment"),
        name="Oversized experiment",
        org_id="org-oversized",
    )
    task = TaskModel(
        id=_id("oversized-task"),
        name="Oversized task",
        org_id=experiment.org_id,
        user="tester",
        task_path=f"s3://tasks/{'x' * OPEN_MAX_BYTES}",
    )
    session.add_all([experiment, task])
    await session.flush()
    await session.execute(
        task_experiments.insert().values(task_id=task.id, experiment_id=experiment.id)
    )
    await session.flush()

    scope = await resolve_member_experiment_read_scope(
        session, experiment_id=experiment.id, org_id=experiment.org_id
    )
    with pytest.raises(HTTPException) as exc_info:
        await get_experiment_open(session, scope=scope)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == (
        "One task shell exceeds the experiment open byte limit"
    )


@pytest.mark.asyncio
async def test_experiment_open_summary_is_exact_before_trial_pages_load(session):
    experiment = ExperimentModel(
        id=_id("summary-experiment"),
        name="Exact summary",
        org_id="org-summary",
    )
    session.add(experiment)
    await session.flush()

    accepted_task = TaskModel(
        id=_id("accepted-task"),
        name="Accepted task",
        org_id=experiment.org_id,
        user="tester",
        task_path="s3://tasks/accepted",
        verdict_status=VerdictStatus.SUCCESS,
        verdict={
            "verdict": "accept",
            "is_good": True,
            "confidence": "high",
            "reasoning": "private detail" * 2_000,
        },
    )
    failed_qa_task = TaskModel(
        id=_id("failed-qa-task"),
        name="Failed QA task",
        org_id=experiment.org_id,
        user="tester",
        task_path="s3://tasks/failed-qa",
        verdict_status=VerdictStatus.FAILED,
    )
    session.add_all([accepted_task, failed_qa_task])
    await session.flush()

    for task in (accepted_task, failed_qa_task):
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

    trial_specs = [
        (accepted_task, "codex", TrialStatus.SUCCESS, 1.0),
        (accepted_task, "codex", TrialStatus.SUCCESS, 0.5),
        # Baselines remain in the outcome distribution but do not affect the
        # per-task mean shown as Avg score.
        (accepted_task, "nop", TrialStatus.SUCCESS, 0.0),
        (failed_qa_task, "codex", TrialStatus.FAILED, None),
    ]
    for index, (task, agent, status, reward) in enumerate(trial_specs):
        session.add(
            TrialModel(
                id=_id(f"summary-trial-{index}"),
                name=f"summary-trial-{index}",
                task_id=task.id,
                task_version_id=task.current_version_id,
                experiment_id=experiment.id,
                org_id=experiment.org_id,
                agent=agent,
                provider="openai",
                queue_key="openai/gpt-5.6",
                model="openai/gpt-5.6",
                status=status,
                reward=reward,
            )
        )
    await session.flush()

    scope = await resolve_member_experiment_read_scope(
        session, experiment_id=experiment.id, org_id=experiment.org_id
    )
    opened = await get_experiment_open(session, scope=scope)

    assert opened.summary.trial_count == 4
    assert opened.summary.pass_count == 1
    assert opened.summary.partial_count == 1
    assert opened.summary.fail_count == 1
    assert opened.summary.harness_error_count == 1
    assert opened.summary.avg_score == pytest.approx(0.75)
    assert opened.summary.qa_accepted == 1
    assert opened.summary.qa_rejected == 0
    assert opened.summary.qa_running == 0
    assert opened.summary.qa_failed == 1
    accepted_shell = next(task for task in opened.tasks if task.id == accepted_task.id)
    assert accepted_shell.verdict is not None
    assert accepted_shell.verdict.verdict == "accept"
    assert accepted_shell.verdict.is_good is True
    assert accepted_shell.verdict.confidence == "high"
    assert "private detail" not in opened.model_dump_json(exclude_none=True)

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from oddish.core.endpoints.experiment_page import (
    _experiment_trial_rows_query,
    get_experiment_trial_page_core,
)
from oddish.core.endpoints.sweep import _plan_append_trials
from oddish.core.sweeps import build_trial_specs_from_sweep
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
)
from oddish.reasoning_effort import configured_reasoning_effort
from oddish.schemas import AgentModelPair, TaskSweepSubmission

MODEL = "global.anthropic.claude-opus-5"


def config(effort):
    return {"agent_config": {"kwargs": {"reasoning_effort": effort}}}


@pytest.mark.parametrize(
    "stored, expected",
    [
        (None, None),
        ({}, None),
        (config(" high "), "high"),
        (config(""), None),
        (config(None), None),
        (config(123), None),
        ({"agent_overrides": {"kwargs": {"reasoning_effort": "low"}}}, "low"),
        (
            {
                **config("xhigh"),
                "agent_overrides": {"kwargs": {"reasoning_effort": "low"}},
            },
            "xhigh",
        ),
        (
            {
                **config(None),
                "agent_overrides": {"kwargs": {"reasoning_effort": "low"}},
            },
            None,
        ),
    ],
)
def test_read_explicit_effort(stored, expected):
    assert configured_reasoning_effort(stored) == expected
    assert TrialModel(harbor_config=stored).reasoning_effort == expected


def submission(task_id="task", *, efforts=("high",), add=False):
    return TaskSweepSubmission(
        task_id=task_id,
        append_to_task=True,
        add_trials=add,
        configs=[
            AgentModelPair(
                agent="claude-code",
                model=MODEL,
                n_trials=5,
                agent_config={"kwargs": {"reasoning_effort": effort}},
            )
            for effort in efforts
        ],
    )


def test_low_trials_do_not_satisfy_high_request():
    trials = build_trial_specs_from_sweep(
        submission(), existing_counts={("claude-code", MODEL, "low"): 5}
    )
    assert len(trials) == 5
    assert {trial.agent_config.kwargs["reasoning_effort"] for trial in trials} == {
        "high"
    }
    assert not build_trial_specs_from_sweep(
        submission(), existing_counts={("claude-code", MODEL, "high"): 5}
    )


def test_effort_is_projected_without_returning_full_configuration():
    query, _ = _experiment_trial_rows_query(experiment_id="e", org_id="o")
    assert "reasoning_effort" in query.selected_columns.keys()
    assert "harbor_config" not in query.selected_columns.keys()
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "agent_overrides" in str(
        query.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "reasoning_effort" in sql


async def seed(session):
    suffix = uuid4().hex[:10]
    exp = ExperimentModel(name=f"effort-{suffix}", org_id="effort-test")
    task = TaskModel(
        name=f"effort-{suffix}", user="test", task_path="test", org_id="effort-test"
    )
    session.add_all([exp, task])
    await session.flush()
    version = TaskVersionModel(
        id=f"{task.id}-v1", task_id=task.id, version=1, task_path="test"
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    trials = []
    for effort in ("low", "high"):
        for i in range(5):
            trial = TrialModel(
                id=f"{task.id}-{effort}-{i}",
                name=f"{effort}-{i}",
                task_id=task.id,
                task_version_id=version.id,
                experiment_id=exp.id,
                org_id="effort-test",
                agent="claude-code",
                provider="anthropic",
                queue_key=MODEL,
                model=MODEL,
                status=TrialStatus.SUCCESS,
                reward=1,
                harbor_config=config(effort),
            )
            trials.append(trial)
    session.add_all(trials)
    await session.flush()
    return task, exp, version, trials


@pytest.mark.asyncio
async def test_sql_matches_python_and_grid_exposes_effort(session):
    task, exp, version, trials = await seed(session)
    trials[0].harbor_config = {
        "agent_overrides": {"kwargs": {"reasoning_effort": "low"}}
    }
    trials[1].harbor_config = {
        **config(None),
        "agent_overrides": {"kwargs": {"reasoning_effort": "low"}},
    }
    await session.flush()
    rows = (
        await session.execute(
            select(TrialModel.id, TrialModel.reasoning_effort).where(
                TrialModel.task_id == task.id
            )
        )
    ).all()
    assert dict(rows) == {trial.id: trial.reasoning_effort for trial in trials}
    page = await get_experiment_trial_page_core(
        session, experiment_id=exp.id, org_id="effort-test"
    )
    assert len(page.trials) == 10
    assert {trial.reasoning_effort for trial in page.trials} == {"low", "high", None}
    assert all("harbor_config" not in trial.model_dump() for trial in page.trials)


@pytest.mark.asyncio
async def test_replacements_stay_with_effort_and_add_mode_creates_new_trials(session):
    task, exp, version, trials = await seed(session)
    low = [trial for trial in trials if trial.reasoning_effort == "low"]
    low[0].status = TrialStatus.FAILED
    await session.flush()

    async def plan(request):
        return await _plan_append_trials(
            session,
            task=task,
            submission=request,
            target_experiment_id=exp.id,
            append_version_id=version.id,
            default_environment=None,
            allowed_environments=None,
        )

    specs, superseded = await plan(submission(task.id, efforts=("high",)))
    assert specs == [] and superseded == []
    specs, superseded = await plan(submission(task.id, efforts=("low", "high")))
    assert len(specs) == 1
    assert specs[0].agent_config.kwargs["reasoning_effort"] == "low"
    assert superseded == [[low[0].id]]
    specs, superseded = await plan(submission(task.id, efforts=("high",), add=True))
    assert len(specs) == 5 and superseded == [[], [], [], [], []]


@pytest.mark.asyncio
async def test_task_summary_and_preview_use_the_same_efforts(session):
    from pathlib import Path
    import runpy
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from oddish.core.endpoints.task_open import get_task_open_core

    # create_all-based test databases need the existing migration-owned view.
    migration = runpy.run_path(
        str(
            Path(__file__).parents[1]
            / "alembic/versions/analysisspend01_create_analysis_spend_view.py"
        )
    )

    def create_view(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration["upgrade"]()

    connection = await session.connection()
    await connection.run_sync(create_view)
    task, _exp, _version, _trials = await seed(session)
    opened = await get_task_open_core(session, task_id=task.id, org_id="effort-test")
    assert opened.selected_version is not None
    groups = opened.selected_version.agent_models
    assert {(group.reasoning_effort, group.trial_count) for group in groups} == {
        ("low", 5),
        ("high", 5),
    }
    assert {trial.reasoning_effort for trial in opened.trials} == {"low", "high"}


@pytest.mark.asyncio
async def test_public_page_exposes_only_effort_and_excludes_probes(session):
    from oddish.core.endpoints.experiment_page import (
        get_public_experiment_trial_page_core,
    )

    task, exp, _version, trials = await seed(session)
    exp.is_public = True
    exp.public_token = uuid4().hex
    trials[0].is_probe = True
    await session.flush()
    page = await get_public_experiment_trial_page_core(
        session, public_token=exp.public_token
    )
    assert len(page.trials) == 9
    assert {trial.reasoning_effort for trial in page.trials} == {"low", "high"}
    assert all("harbor_config" not in trial.model_dump() for trial in page.trials)


def test_existing_request_hash_survives_new_add_trials_default():
    from types import SimpleNamespace
    from oddish.core.idempotency import compute_request_hash

    request = submission()
    legacy_payload = request.model_dump(mode="json")
    legacy_payload.pop("add_trials")
    old_request = SimpleNamespace(
        model_dump=lambda **kwargs: legacy_payload.copy(),
        registry_auth=request.registry_auth,
    )
    assert compute_request_hash(request) == compute_request_hash(old_request)
    assert compute_request_hash(submission(add=True)) != compute_request_hash(request)

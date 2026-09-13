"""Statements per request, pinned.

Every dashboard request pays one network round trip per statement, and the
database sits a network hop away from the API containers, so the number of
statements a core issues is the latency budget of its endpoint. These tests
seed one small task and count the statements each read core issues on the
steady-state path (cost-exclusion cache warm). Raise a budget only with a
reason in the diff; lowering one is free.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import event, insert

from oddish.core.cost_exclusions import load_cost_exclusions
from oddish.core.endpoints import (
    browse_tasks_core,
    get_experiment_open_core,
    get_experiment_trial_page_core,
    get_task_detail_core,
    get_task_status_core,
    get_trial_response_for_org_core,
)
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    experiment_trials,
    generate_id,
    task_experiments,
    utcnow,
)

_ORG = f"budget-org-{uuid.uuid4().hex[:8]}"

# Steady-state statement counts per core (cost exclusions cached, no auth),
# measured 2026-09-03 on the seed below. Before the request-path work the
# same calls issued 11, 9, 15, 4, 4 and 7 statements: SAVEPOINT + three
# cost-exclusion SELECTs + RELEASE on every call, and two worker-job queries
# instead of one.
BUDGETS = {
    "get_task_status_core": 5,
    "get_trial_response_for_org_core": 3,
    "get_task_detail_core": 9,
    "browse_tasks_core": 4,
    "get_experiment_open_core": 4,
    "get_experiment_trial_page_core": 2,
}


@contextmanager
def count_statements():
    import oddish.db.connection as conn

    seen: list[str] = []

    def _record(_conn, _cursor, statement, *_):
        seen.append(statement)

    event.listen(conn.engine.sync_engine, "after_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(conn.engine.sync_engine, "after_cursor_execute", _record)


async def _seed(session):
    task = TaskModel(
        name=f"budget-task-{_ORG}",
        org_id=_ORG,
        user="tester",
        task_path="s3://tasks/budget",
    )
    experiment = ExperimentModel(
        name=f"budget-exp-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add_all([task, experiment])
    await session.flush()
    trials = []
    for status in (TrialStatus.SUCCESS, TrialStatus.RUNNING, TrialStatus.QUEUED):
        trial_id = generate_id()
        trials.append(
            TrialModel(
                id=trial_id,
                name=trial_id,
                task_id=task.id,
                experiment_id=experiment.id,
                org_id=_ORG,
                agent="claude-code",
                provider="anthropic",
                queue_key="anthropic/claude-opus-4-8",
                model="claude-opus-4-8",
                status=status,
                created_at=utcnow(),
            )
        )
    session.add_all(trials)
    await session.flush()
    await session.execute(
        insert(task_experiments).values(
            [{"task_id": task.id, "experiment_id": experiment.id}]
        )
    )
    await session.execute(
        insert(experiment_trials).values(
            [{"experiment_id": experiment.id, "trial_id": t.id} for t in trials]
        )
    )
    session.add_all(
        [
            WorkerJobModel(
                id=generate_id(),
                kind=WorkerJobKind.TRIAL,
                status=WorkerJobStatus.RUNNING,
                queue_key="anthropic/claude-opus-4-8",
                subject_table="trials",
                subject_id=trials[1].id,
                attempts=1,
                max_attempts=3,
                created_at=utcnow(),
            )
        ]
    )
    await session.flush()
    await load_cost_exclusions(session)  # warm the per-process cache
    return task, experiment, trials


async def _measure(session, name, call):
    with count_statements() as statements:
        await call()
    assert len(statements) <= BUDGETS[name], (
        f"{name} issued {len(statements)} statements, budget {BUDGETS[name]}:\n"
        + "\n".join(f"  {i + 1}. {s[:110]}" for i, s in enumerate(statements))
    )
    return len(statements)


@pytest.mark.asyncio
async def test_task_and_trial_reads_stay_within_budget(session):
    task, _, trials = await _seed(session)

    await _measure(
        session,
        "get_task_status_core",
        lambda: get_task_status_core(session, task_id=task.id, org_id=_ORG),
    )
    await _measure(
        session,
        "get_trial_response_for_org_core",
        lambda: get_trial_response_for_org_core(
            session, trial_id=trials[0].id, org_id=_ORG
        ),
    )
    await _measure(
        session,
        "get_task_detail_core",
        lambda: get_task_detail_core(session, task_id=task.id, org_id=_ORG),
    )
    await _measure(
        session,
        "browse_tasks_core",
        lambda: browse_tasks_core(session, org_id=_ORG, limit=25),
    )


@pytest.mark.asyncio
async def test_experiment_page_reads_stay_within_budget(session):
    _, experiment, _ = await _seed(session)

    await _measure(
        session,
        "get_experiment_open_core",
        lambda: get_experiment_open_core(
            session, experiment_id=experiment.id, org_id=_ORG
        ),
    )
    await _measure(
        session,
        "get_experiment_trial_page_core",
        lambda: get_experiment_trial_page_core(
            session, experiment_id=experiment.id, org_id=_ORG
        ),
    )

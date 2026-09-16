"""Read real versioned trials: preserve audit provenance and bounded SQL reads."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

from oddish.core.endpoints.trials import (
    get_trial_by_index_core,
    get_trial_response_for_org_core,
)
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    utcnow,
)
from test_statement_budgets import count_statements

_FINDING = {"tier": "must_fix", "title": "Verifier ignores stderr"}


@pytest_asyncio.fixture
async def versioned_trial(session):
    task_id = f"audit-{uuid.uuid4().hex[:8]}"
    task = TaskModel(
        id=task_id, name=task_id, task_path="/tmp/task", org_id="audit-org", user="test"
    )
    experiment = ExperimentModel(id=task_id, name=task_id, org_id=task.org_id)
    session.add_all([task, experiment])
    await session.flush()
    original = TaskVersionModel(
        id=f"{task_id}-v1",
        task_id=task_id,
        version=1,
        task_path="/tmp/v1",
        pre_trial={"items": [_FINDING], "cost_usd": 0.25},
        pre_trial_status=VerdictStatus.SUCCESS,
    )
    current = TaskVersionModel(
        id=f"{task_id}-v2",
        task_id=task_id,
        version=2,
        task_path="/tmp/v2",
    )
    session.add_all([original, current])
    await session.flush()
    task.current_version_id = current.id
    trial = TrialModel(
        id=f"{task_id}-0",
        name=f"{task_id}-0",
        task_id=task_id,
        task_version_id=original.id,
        experiment_id=experiment.id,
        org_id=task.org_id,
        agent="claude-code",
        provider="anthropic",
        queue_key="anthropic/test",
        status=TrialStatus.SUCCESS,
    )
    session.add(trial)
    await session.flush()
    return task, original, current, trial


@pytest.fixture(
    params=[get_trial_response_for_org_core, get_trial_by_index_core],
    ids=["by-id", "by-index"],
)
def read_trial(request, session, versioned_trial):
    task, _, _, trial = versioned_trial
    kwargs = (
        {"trial_id": trial.id}
        if request.param is get_trial_response_for_org_core
        else {"task_id": task.id, "index": 0}
    )

    async def read(org_id="audit-org"):
        return await request.param(session, org_id=org_id, **kwargs)

    return read


@pytest.mark.asyncio
async def test_original_audit_and_full_response_survive_cold_and_warm_reads(
    read_trial, session
):
    # Evict seeded ORM objects so an identity-map hit cannot hide a lookup.
    session.expunge_all()
    with count_statements() as cold:
        first = await read_trial()
    with count_statements() as warm:
        second = await read_trial()
    # This fixture uses a write transaction: exclude optional-read SAVEPOINTs.
    assert len([sql for sql in cold if sql.lstrip().upper().startswith("SELECT")]) == 4
    assert len([sql for sql in warm if sql.lstrip().upper().startswith("SELECT")]) == 3
    assert first.model_dump() == second.model_dump()
    assert first.pre_trial_findings == [_FINDING]
    assert first.pre_trial_status == "success"
    assert first.pre_trial_cost_usd == 0.25
    assert first.pre_trial_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version_state", ["current", "unversioned", "deleted", "failed"]
)
async def test_audit_edge_cases(read_trial, session, versioned_trial, version_state):
    _, original, current, trial = versioned_trial
    if version_state == "current":
        trial.task_version_id = current.id
    elif version_state == "unversioned":
        trial.task_version_id = None
    elif version_state == "deleted":
        original.deleted_at = utcnow()
    else:
        original.pre_trial = None
        original.pre_trial_status = VerdictStatus.FAILED
        original.pre_trial_error = "TimeoutError()"
    await session.flush()
    session.expunge_all()
    response = await read_trial()
    if version_state == "deleted":
        # TaskVersionModel is not registered for automatic soft-delete filtering;
        # the old session.get reader likewise preserved historical audit data.
        assert response.pre_trial_findings == [_FINDING]
        assert response.pre_trial_status == "success"
        assert response.pre_trial_cost_usd == 0.25
        return
    assert response.pre_trial_findings == []
    assert response.pre_trial_cost_usd is None
    assert response.pre_trial_status == (
        "failed" if version_state == "failed" else None
    )
    assert response.pre_trial_error == (
        "TimeoutError()" if version_state == "failed" else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", ["other-org", "deleted-task", "deleted-trial"])
async def test_trial_visibility_is_preserved(
    read_trial, session, versioned_trial, hidden
):
    task, _, _, trial = versioned_trial
    if hidden == "deleted-task":
        task.deleted_at = utcnow()
    elif hidden == "deleted-trial":
        trial.deleted_at = utcnow()
    await session.flush()
    with pytest.raises(HTTPException) as error:
        await read_trial(org_id="other-org" if hidden == "other-org" else task.org_id)
    assert error.value.status_code == 404

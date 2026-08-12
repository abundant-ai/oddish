from __future__ import annotations

import uuid

import pytest

from oddish.core.endpoints.task_open import get_task_open_core
from oddish.db.models import (
    ExperimentModel,
    TagAssignmentModel,
    TagAssignmentScope,
    TagModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    generate_id,
    utcnow,
)


@pytest.mark.asyncio
async def test_cache_only_tokens_are_consistently_unpriced(session) -> None:
    org_id = f"task-open-cost-{uuid.uuid4().hex[:8]}"
    task = TaskModel(
        name=f"cache-only-{org_id}",
        org_id=org_id,
        user="tester",
        task_path=f"s3://tasks/{org_id}",
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
    experiment = ExperimentModel(
        name=f"cache-only-exp-{org_id}",
        org_id=org_id,
        last_activity_at=utcnow(),
    )
    session.add(experiment)
    await session.flush()

    trial_id = generate_id()
    session.add(
        TrialModel(
            id=trial_id,
            name=trial_id,
            task_id=task.id,
            task_version_id=version.id,
            experiment_id=experiment.id,
            org_id=org_id,
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5.5-codex",
            model="gpt-5.5-codex",
            status=TrialStatus.SUCCESS,
            input_tokens=0,
            output_tokens=0,
            cache_tokens=1_000_000,
            cache_write_tokens=0,
            cost_usd=None,
            created_at=utcnow(),
        )
    )
    await session.flush()

    response = await get_task_open_core(session, task_id=task.id, org_id=org_id)

    assert response.selected_version is not None
    assert response.selected_version.cost_usd == 0
    assert response.selected_version.cost_trial_count == 0
    assert response.selected_version.cost_has_estimated is False
    assert response.totals.cost_trial_count == 0
    assert response.totals.cost_has_estimated is False
    assert response.trials[0].cost_usd is None
    assert response.trials[0].cost_is_estimated is None

    task.current_version_id = None
    await session.flush()
    legacy_response = await get_task_open_core(session, task_id=task.id, org_id=org_id)
    assert legacy_response.task.status == "completed"
    assert legacy_response.default_version is None
    assert legacy_response.selected_version is None


@pytest.mark.asyncio
async def test_selected_historical_version_returns_its_direct_tags(session) -> None:
    org_id = f"task-open-tags-{uuid.uuid4().hex[:8]}"
    task = TaskModel(
        name=f"version-tags-{org_id}",
        org_id=org_id,
        user="tester",
        task_path=f"s3://tasks/{org_id}/v2",
    )
    session.add(task)
    await session.flush()

    historical = TaskVersionModel(
        id=f"{task.id}-v1",
        task_id=task.id,
        version=1,
        task_path=f"s3://tasks/{org_id}/v1",
    )
    current = TaskVersionModel(
        id=f"{task.id}-v2",
        task_id=task.id,
        version=2,
        task_path=task.task_path,
    )
    historical_tag = TagModel(
        id=generate_id(),
        org_id=org_id,
        key="historical",
        normalized_key="historical",
        color="#123456",
    )
    current_tag = TagModel(
        id=generate_id(),
        org_id=org_id,
        key="current",
        normalized_key="current",
        color="#654321",
    )
    session.add_all([historical, current, historical_tag, current_tag])
    await session.flush()
    task.current_version_id = current.id
    session.add_all(
        [
            TagAssignmentModel(
                id=generate_id(),
                tag_id=historical_tag.id,
                org_id=org_id,
                scope=TagAssignmentScope.VERSION,
                target_id=historical.id,
                task_id=task.id,
            ),
            TagAssignmentModel(
                id=generate_id(),
                tag_id=current_tag.id,
                org_id=org_id,
                scope=TagAssignmentScope.VERSION,
                target_id=current.id,
                task_id=task.id,
            ),
        ]
    )
    await session.flush()

    response = await get_task_open_core(
        session,
        task_id=task.id,
        version_id=historical.id,
        org_id=org_id,
    )

    assert response.selected_version is not None
    assert response.selected_version.id == historical.id
    assert [tag.key for tag in response.selected_version.user_tags] == ["historical"]
    selected_payload = response.model_dump()["selected_version"]
    assert "pre_trial_findings" not in selected_payload
    assert "pre_trial_status" not in selected_payload
    assert "pre_trial_error" not in selected_payload
    assert "pre_trial_cost_usd" not in selected_payload

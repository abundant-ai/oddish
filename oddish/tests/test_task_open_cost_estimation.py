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
async def test_selected_version_returns_only_its_direct_tags(session) -> None:
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

    current_response = await get_task_open_core(session, task_id=task.id, org_id=org_id)
    historical_response = await get_task_open_core(
        session,
        task_id=task.id,
        version_id=historical.id,
        org_id=org_id,
    )

    assert current_response.selected_version is not None
    assert historical_response.selected_version is not None
    assert [tag.key for tag in current_response.selected_version.user_tags] == [
        "current"
    ]
    assert [tag.key for tag in historical_response.selected_version.user_tags] == [
        "historical"
    ]


@pytest.mark.asyncio
async def test_qa_spend_direct_and_historical_attribution(session) -> None:
    """The indexed branches preserve the old row-level OR, including old ledgers."""
    from sqlalchemy import text
    from oddish.core.endpoints.task_open_queries import AGGREGATE_SQL
    from oddish.db.models import AnalysisCostModel

    task_id = f"spend-{uuid.uuid4().hex}"
    task = TaskModel(
        id=task_id,
        name=task_id,
        org_id="spend-org",
        user="tester",
        task_path="s3://task",
    )
    session.add(task)
    await session.flush()
    experiment = ExperimentModel(name=task_id, org_id=task.org_id)
    session.add(experiment)
    await session.flush()
    trial_id = f"{task_id}-trial"
    session.add(
        TrialModel(
            id=trial_id,
            name=trial_id,
            task_id=task_id,
            experiment_id=experiment.id,
            org_id="spend-org",
            agent="codex",
            provider="openai",
            queue_key="test",
            kind="audit",
            status=TrialStatus.SUCCESS,
            cost_usd=2,
        )
    )
    await session.flush()
    # Direct-only, indirect-only, both, mismatched direct task, orphan,
    # foreign-org, voided, null cost, and a ledger referencing a deleted trial.
    for direct, linked, org, cost, deleted in [
        (task_id, None, "spend-org", 3, False),
        (None, trial_id, "spend-org", 5, False),
        (task_id, trial_id, "spend-org", 7, False),
        ("other", trial_id, "spend-org", 11, False),
        (task_id, "orphan", "spend-org", 13, False),
        (task_id, trial_id, "foreign", 17, False),
        (task_id, trial_id, "spend-org", 19, True),
        (None, trial_id, "spend-org", None, False),
    ]:
        session.add(
            AnalysisCostModel(
                job_kind="analysis",
                cost_source="native",
                task_id=direct,
                trial_id=linked,
                org_id=org,
                cost_usd=cost,
                deleted_at=utcnow() if deleted else None,
            )
        )
    await session.flush()
    old = text("""SELECT COALESCE(sum(a.cost_usd), 0) FROM analysis_spend a
        LEFT JOIN trials qat ON qat.id = a.trial_id
        WHERE (a.task_id = :task_id OR qat.task_id = :task_id)
        AND (CAST(:org_id AS text) IS NULL OR a.org_id = :org_id)""")
    for deleted in (False, True):
        if deleted:
            await session.execute(
                text("UPDATE trials SET deleted_at = now() WHERE id = :id"),
                {"id": trial_id},
            )
        for org in ("spend-org", "foreign", None):
            params = dict(
                task_id=task_id, org_id=org, version_id=None, current_version_id=None
            )
            expected = (await session.execute(old, params)).scalar_one()
            actual = (
                (await session.execute(AGGREGATE_SQL, params))
                .mappings()
                .one()["qa_cost_usd"]
            )
            assert actual == pytest.approx(expected)
            if org == "spend-org":
                assert actual == pytest.approx(39 if deleted else 41)

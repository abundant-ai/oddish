"""Latest-runner projection schema and PostgreSQL trigger behavior."""

from __future__ import annotations

from datetime import timedelta
import os
import uuid

import pytest
from sqlalchemy import select, update

from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    experiment_trials,
    get_session,
    utcnow,
)


def test_latest_runner_indexes_match_dashboard_seek() -> None:
    indexes = {index.name: index for index in ExperimentModel.__table__.indexes}

    search_index = indexes["idx_experiments_org_last_runner_activity_live"]
    assert [str(expression) for expression in search_index.expressions] == [
        "experiments.org_id",
        "experiments.last_runner_user_id",
        "last_activity_at DESC NULLS LAST",
        "id ASC",
    ]
    assert "last_runner_user_id IS NOT NULL" in str(
        search_index.dialect_options["postgresql"]["where"]
    )
    assert "shadow_of IS NULL" in str(
        search_index.dialect_options["postgresql"]["where"]
    )


@pytest.mark.asyncio
async def test_latest_runner_triggers_move_forward_and_backward() -> None:
    if not os.environ.get("ODDISH_DATABASE_URL"):
        pytest.skip("ODDISH_DATABASE_URL not set")

    run = uuid.uuid4().hex[:10]
    task_id = f"runner-task-{run}"
    now = utcnow()

    async with get_session() as session:
        home = ExperimentModel(name=f"runner-home-{run}")
        collection = ExperimentModel(
            name=f"runner-collection-{run}", is_collection=True
        )
        task = TaskModel(
            id=task_id,
            name=task_id,
            user="runner-test",
            task_path="s3://test-bucket/latest-runner",
        )
        session.add_all([home, collection, task])
        await session.flush()

        older = TrialModel(
            id=f"{task_id}-1",
            name=f"{task_id}-1",
            task_id=task.id,
            experiment_id=home.id,
            billed_user_id="user-older",
            agent="nop",
            provider="nop",
            queue_key="nop",
            created_at=now - timedelta(minutes=1),
        )
        newer = TrialModel(
            id=f"{task_id}-2",
            name=f"{task_id}-2",
            task_id=task.id,
            experiment_id=home.id,
            billed_user_id="user-newer",
            agent="nop",
            provider="nop",
            queue_key="nop",
            created_at=now,
        )
        session.add_all([older, newer])
        await session.flush()
        await session.refresh(home)
        assert home.last_runner_trial_id == newer.id
        assert home.last_runner_user_id == "user-newer"

        await session.execute(
            experiment_trials.insert(),
            [
                {"experiment_id": collection.id, "trial_id": older.id},
                {"experiment_id": collection.id, "trial_id": newer.id},
            ],
        )
        await session.refresh(collection)
        assert collection.last_runner_trial_id == newer.id

        # Superseding the newest source moves both projections backward.
        newer.superseded_by_trial_id = older.id
        await session.flush()
        await session.refresh(home)
        await session.refresh(collection)
        assert home.last_runner_trial_id == older.id
        assert collection.last_runner_trial_id == older.id

        # Restoring the trial moves home forward; removing only its collection
        # membership then moves the collection backward independently.
        newer.superseded_by_trial_id = None
        await session.flush()
        await session.execute(
            update(experiment_trials)
            .where(
                experiment_trials.c.experiment_id == collection.id,
                experiment_trials.c.trial_id == newer.id,
            )
            .values(deleted_at=utcnow())
        )
        await session.refresh(home)
        await session.refresh(collection)
        assert home.last_runner_trial_id == newer.id
        assert collection.last_runner_trial_id == older.id

    # Use hard deletes so the test leaves no rows in a shared development DB.
    async with get_session() as session:
        experiment_ids = (
            await session.scalars(
                select(ExperimentModel.id).where(
                    ExperimentModel.name.in_(
                        (f"runner-home-{run}", f"runner-collection-{run}")
                    )
                )
            )
        ).all()
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id.in_(experiment_ids)
            )
        )

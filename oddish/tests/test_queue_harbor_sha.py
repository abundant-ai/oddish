import pytest
from sqlalchemy import select

from oddish.db import TaskModel, TrialModel, get_session
from oddish.queue import _bulk_insert_trials, get_or_create_experiment

pytestmark = pytest.mark.asyncio


async def test_bulk_insert_persists_harbor_sha():
    async with get_session() as session:
        # Idempotent setup: drop any leftover row (ON DELETE CASCADE removes the
        # trial) so the fixed-id test is re-runnable against a persistent DB.
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == "hsha-task")
        )
        await session.flush()
        session.add(
            TaskModel(
                id="hsha-task",
                name="hsha-task",
                user="t",
                org_id=None,
                task_path="p",
            )
        )
        await session.flush()
        experiment = await get_or_create_experiment(session, name="hsha-exp")
        await session.flush()
        await _bulk_insert_trials(
            session,
            [
                {
                    "id": "hsha-task-0",
                    "name": "hsha-task-0",
                    "task_id": "hsha-task",
                    "task_version_id": None,
                    "experiment_id": experiment.id,
                    "org_id": None,
                    "agent": "nop",
                    "provider": "nop_oracle",
                    "queue_key": "nop_oracle",
                    "model": None,
                    "timeout_minutes": None,
                    "environment": None,
                    "harbor_config": {
                        "resolved_sha": "d" * 40,
                        "variant_id": "default",
                    },
                    "harbor_sha": "d" * 40,
                    "is_probe": False,
                    "max_attempts": 6,
                }
            ],
        )
        row = (
            await session.execute(
                select(TrialModel.harbor_sha).where(TrialModel.id == "hsha-task-0")
            )
        ).scalar_one()
        assert row == "d" * 40

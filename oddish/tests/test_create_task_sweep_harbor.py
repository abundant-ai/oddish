import pytest
from fastapi import HTTPException

from oddish.config import HARBOR_DEFAULT_SHA
from oddish.core.endpoints import create_task_sweep_core
from oddish.db import TaskModel, get_session
from oddish.schemas import AgentModelPair, HarborConfig, TaskSweepSubmission

pytestmark = pytest.mark.asyncio


def _submission(**harbor_kw):
    return TaskSweepSubmission(
        task_id="hsweep-task",
        name="hsweep-task",
        configs=[AgentModelPair(agent="nop", n_trials=1)],
        harbor=HarborConfig(**harbor_kw),
    )


async def _cleanup(session, *task_ids):
    # Idempotent setup: ON DELETE CASCADE removes trials so the fixed-id tests
    # are re-runnable against a persistent DB.
    for tid in task_ids:
        await session.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))
    await session.flush()


async def test_default_submission_stamps_default_sha():
    async with get_session() as session:
        await _cleanup(session, "hsweep-task")
        session.add(
            TaskModel(
                id="hsweep-task",
                name="hsweep-task",
                user="t",
                org_id=None,
                task_path="p",
            )
        )
        await session.flush()
        _task, trials, _is_append, _exp = await create_task_sweep_core(
            session, submission=_submission()
        )
        assert trials and all(t.harbor_sha == HARBOR_DEFAULT_SHA for t in trials)


async def test_allowlisted_non_default_pin_is_stamped_on_trials_and_jobs(monkeypatch):
    import oddish.core.harbor_source as hs
    from sqlalchemy import select

    from oddish.db import WorkerJobModel

    monkeypatch.setattr(
        hs, "resolve_harbor_pin", lambda s, r: hs.ResolvedPin(s, "c" * 40)
    )
    async with get_session() as session:
        await _cleanup(session, "hsweep-task2")
        session.add(
            TaskModel(
                id="hsweep-task2",
                name="hsweep-task2",
                user="t",
                org_id=None,
                task_path="p",
            )
        )
        await session.flush()
        _task, trials, _is_append, _exp = await create_task_sweep_core(
            session,
            submission=TaskSweepSubmission(
                task_id="hsweep-task2",
                configs=[AgentModelPair(agent="nop", n_trials=1)],
                harbor=HarborConfig(
                    source="https://github.com/dot-agi/harbor", ref="main"
                ),
            ),
        )
        # The allowed override is resolved + stamped (no longer rejected), and the
        # ephemeral variant rides onto the worker_jobs dispatch key.
        assert trials and all(t.harbor_sha == "c" * 40 for t in trials)
        variants = (
            await session.execute(
                select(WorkerJobModel.harbor_variant_id).where(
                    WorkerJobModel.subject_id.in_([t.id for t in trials])
                )
            )
        ).scalars().all()
        assert variants and all(v == "ephemeral" for v in variants)


async def test_disallowed_source_rejected_with_422():
    async with get_session() as session:
        await _cleanup(session, "hsweep-task3")
        session.add(
            TaskModel(
                id="hsweep-task3",
                name="hsweep-task3",
                user="t",
                org_id=None,
                task_path="p",
            )
        )
        await session.flush()
        with pytest.raises(HTTPException) as exc:
            await create_task_sweep_core(
                session,
                submission=TaskSweepSubmission(
                    task_id="hsweep-task3",
                    configs=[AgentModelPair(agent="nop", n_trials=1)],
                    harbor=HarborConfig(
                        source="https://github.com/evil/harbor", ref="main"
                    ),
                ),
            )
        assert exc.value.status_code == 422

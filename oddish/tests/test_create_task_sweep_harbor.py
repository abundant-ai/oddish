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
            (
                await session.execute(
                    select(WorkerJobModel.harbor_variant_id).where(
                        WorkerJobModel.subject_id.in_([t.id for t in trials])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert variants and all(v == "ephemeral" for v in variants)


async def test_gke_environment_routes_trials_to_the_gke_variant():
    # A GKE submission with no explicit Harbor source must dispatch onto the
    # blessed gke variant (harbor-gke image), not default or ephemeral.
    from sqlalchemy import select

    from oddish.db import WorkerJobModel
    from harbor.models.environment_type import EnvironmentType

    async with get_session() as session:
        await _cleanup(session, "hsweep-gke")
        session.add(
            TaskModel(
                id="hsweep-gke",
                name="hsweep-gke",
                user="t",
                org_id=None,
                task_path="p",
            )
        )
        await session.flush()
        _task, trials, _is_append, _exp = await create_task_sweep_core(
            session,
            submission=TaskSweepSubmission(
                task_id="hsweep-gke",
                configs=[AgentModelPair(agent="nop", n_trials=1)],
                harbor=HarborConfig(),
                environment=EnvironmentType.GKE,
            ),
        )
        assert trials
        variants = (
            (
                await session.execute(
                    select(WorkerJobModel.harbor_variant_id).where(
                        WorkerJobModel.subject_id.in_([t.id for t in trials])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert variants and all(v == "gke" for v in variants)


async def test_non_gke_environment_stays_on_default_variant():
    # The mirror of the GKE case: a non-GKE submission must never be pointed at
    # harbor-gke -- it keeps the default variant.
    from sqlalchemy import select

    from oddish.db import WorkerJobModel
    from harbor.models.environment_type import EnvironmentType

    async with get_session() as session:
        await _cleanup(session, "hsweep-daytona")
        session.add(
            TaskModel(
                id="hsweep-daytona",
                name="hsweep-daytona",
                user="t",
                org_id=None,
                task_path="p",
            )
        )
        await session.flush()
        _task, trials, _is_append, _exp = await create_task_sweep_core(
            session,
            submission=TaskSweepSubmission(
                task_id="hsweep-daytona",
                configs=[AgentModelPair(agent="nop", n_trials=1)],
                harbor=HarborConfig(),
                environment=EnvironmentType.DAYTONA,
            ),
        )
        assert trials and all(t.harbor_sha == HARBOR_DEFAULT_SHA for t in trials)
        variants = (
            (
                await session.execute(
                    select(WorkerJobModel.harbor_variant_id).where(
                        WorkerJobModel.subject_id.in_([t.id for t in trials])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert variants and all(v == "default" for v in variants)


async def test_effective_sweep_environment_resolution_is_db_free():
    # Bug B core logic, provable WITHOUT a DB (the append integration test below
    # is DB-gated). The harbor stamp must resolve the SAME environment as
    # build_trial_specs_from_sweep: an explicit submission override wins, else the
    # environment inherited from an append target's existing trials, else the
    # caller-resolved default.
    from harbor.models.environment_type import EnvironmentType as E

    from oddish.core.endpoints.sweep import _effective_sweep_environment

    # Submission override wins over inherited + default.
    assert _effective_sweep_environment(E.GKE, E.DAYTONA, E.MODAL) == E.GKE
    # No override -> inherit the append target's environment. THIS is the Bug B
    # guard: a GKE task appended-to without --env still resolves GKE, so the stamp
    # binds harbor-gke instead of leaving the trial on the lean default image.
    assert _effective_sweep_environment(None, E.GKE, E.MODAL) == E.GKE
    # No override, nothing inherited (create, or empty append target) -> default.
    assert _effective_sweep_environment(None, None, E.MODAL) == E.MODAL
    # Nothing anywhere -> None (the stamp is skipped entirely).
    assert _effective_sweep_environment(None, None, None) is None


async def test_append_without_env_inherits_gke_and_routes_to_gke_variant():
    # Bug B regression: appending trials to an existing GKE task WITHOUT passing
    # --env must still dispatch onto the gke variant. The appended trials inherit
    # the task's GKE environment, so the harbor stamp must see GKE too -- otherwise
    # they classify 'default' and silently run the lean default image.
    from harbor.models.environment_type import EnvironmentType
    from sqlalchemy import select

    from oddish.db import WorkerJobModel

    async with get_session() as session:
        await _cleanup(session, "hsweep-gke-append")
        session.add(
            TaskModel(
                id="hsweep-gke-append",
                name="hsweep-gke-append",
                user="t",
                org_id=None,
                task_path="p",
            )
        )
        await session.flush()
        # Seed the task with a GKE trial (submission carries --env gke).
        await create_task_sweep_core(
            session,
            submission=TaskSweepSubmission(
                task_id="hsweep-gke-append",
                configs=[AgentModelPair(agent="nop", n_trials=1)],
                harbor=HarborConfig(),
                environment=EnvironmentType.GKE,
            ),
        )
        # Append WITHOUT an environment (distinct model so it is not reconciled
        # away against the seeded trial); it must inherit GKE.
        _task, new_trials, is_append, _exp = await create_task_sweep_core(
            session,
            submission=TaskSweepSubmission(
                task_id="hsweep-gke-append",
                configs=[
                    AgentModelPair(agent="nop", model="distinct-model", n_trials=1)
                ],
                harbor=HarborConfig(),
            ),
        )
        assert is_append and new_trials
        variants = (
            (
                await session.execute(
                    select(WorkerJobModel.harbor_variant_id).where(
                        WorkerJobModel.subject_id.in_([t.id for t in new_trials])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert variants and all(v == "gke" for v in variants)


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

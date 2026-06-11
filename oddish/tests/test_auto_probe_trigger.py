"""maybe_enqueue_auto_probe enqueues exactly one probe per task version,
rotates models across versions, and never raises into the caller.

Also includes an end-to-end test verifying that create_task_sweep_core
triggers the auto-probe and that dedup holds across a second submission.
"""
from __future__ import annotations

from pathlib import Path
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.auto_probe import maybe_enqueue_auto_probe  # noqa: E402
from oddish.db import TaskModel, TrialModel, get_session  # noqa: E402
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import AgentModelPair, TaskSubmission, TaskSweepSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]


@pytest_asyncio.fixture
async def cleanup_task_ids():
    ids: list[str] = []
    yield ids
    async with get_session() as s:
        for tid in ids:
            # ON DELETE CASCADE removes trials + task_versions with the task.
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))


async def _probe_trials(session, task_id: str) -> list:
    rows = await session.execute(
        TrialModel.__table__.select().where(
            (TrialModel.task_id == task_id) & (TrialModel.is_probe.is_(True))
        )
    )
    return list(rows)


@pytest.mark.asyncio
async def test_enqueues_one_probe_and_is_idempotent(cleanup_task_ids):
    async with get_session() as session:
        task = await create_task(
            session,
            TaskSubmission(
                name=f"auto-probe-one-{_RUN}",
                task_path="s3://test-bucket/auto-probe-fake-task",
                user="test",
                trials=[TrialSpec(agent="nop", model=None)],
            ),
        )
    cleanup_task_ids.append(task.id)

    async with get_session() as session:
        task = await session.get(TaskModel, task.id)
        # create_task auto-links an experiment via the sweep path, so experiment_id=None is safe here.
        await maybe_enqueue_auto_probe(session, task=task, experiment=None, org_id=None)
    async with get_session() as session:
        first = await _probe_trials(session, task.id)
    assert len(first) == 1
    assert first[0].is_probe is True

    async with get_session() as session:
        task = await session.get(TaskModel, task.id)
        await maybe_enqueue_auto_probe(session, task=task, experiment=None, org_id=None)
    async with get_session() as session:
        second = await _probe_trials(session, task.id)
    assert len(second) == 1


@pytest.mark.asyncio
async def test_failure_is_swallowed(cleanup_task_ids, monkeypatch):
    async with get_session() as session:
        task = await create_task(
            session,
            TaskSubmission(
                name=f"auto-probe-fail-{_RUN}",
                task_path="s3://test-bucket/auto-probe-fake-task",
                user="test",
                trials=[TrialSpec(agent="nop", model=None)],
            ),
        )
    cleanup_task_ids.append(task.id)

    import oddish.core.auto_probe as ap

    def boom(*a, **k):
        raise RuntimeError("enqueue blew up")

    monkeypatch.setattr(ap, "build_trial_specs_from_sweep", boom)

    async with get_session() as session:
        task = await session.get(TaskModel, task.id)
        await maybe_enqueue_auto_probe(session, task=task, experiment=None, org_id=None)  # must NOT raise


@pytest.mark.asyncio
async def test_no_probe_when_no_current_version(cleanup_task_ids):
    async with get_session() as session:
        task = await create_task(
            session,
            TaskSubmission(
                name=f"auto-probe-noversion-{_RUN}",
                task_path="s3://test-bucket/auto-probe-fake-task",
                user="test",
                trials=[TrialSpec(agent="nop", model=None)],
            ),
        )
    cleanup_task_ids.append(task.id)

    async with get_session() as session:
        task = await session.get(TaskModel, task.id)
        task.current_version_id = None  # simulate a task with no resolved version
        await maybe_enqueue_auto_probe(session, task=task, experiment=None, org_id=None)
    async with get_session() as session:
        assert len(await _probe_trials(session, task.id)) == 0


# ---------------------------------------------------------------------------
# Integration: auto-probe fires from create_task_sweep_core
# ---------------------------------------------------------------------------

_SWEEP_RUN = uuid.uuid4().hex[:8]


@pytest_asyncio.fixture
async def seeded_sweep_task_id(cleanup_task_ids):
    """Seed a TaskModel (with version + experiment) using create_task so that
    current_version_id is set and append_trials_to_task can find linked
    experiments. Cleans up via cleanup_task_ids."""
    task_id = f"test-auto-probe-sweep-{_SWEEP_RUN}"
    async with get_session() as s:
        # Use create_task (not raw insert) so the task gets current_version_id
        # and a linked experiment — both required for the append path and for
        # maybe_enqueue_auto_probe to actually enqueue.
        await create_task(
            s,
            TaskSubmission(
                name=f"auto-probe-sweep-{_SWEEP_RUN}",
                task_path="s3://test-bucket/auto-probe-fake-task",
                user="test",
                trials=[TrialSpec(agent="nop", model=None)],
            ),
            task_id=task_id,
        )
    cleanup_task_ids.append(task_id)
    yield task_id


@pytest.mark.asyncio
async def test_sweep_triggers_auto_probe_when_opted_in(seeded_sweep_task_id):
    """With ``run_probe=True``, create_task_sweep_core must enqueue exactly one
    probe trial on first sweep and still exactly one on a second sweep (dedup
    holds end-to-end). Auto-probe is opt-in: it only fires when run_probe is set."""
    from oddish.core.endpoints import create_task_sweep_core

    submission = TaskSweepSubmission(
        task_id=seeded_sweep_task_id,
        append_to_task=True,
        configs=[
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=1,
            )
        ],
        user="test",
        run_probe=True,
    )

    # First sweep: probe should be enqueued.
    async with get_session() as s:
        await create_task_sweep_core(s, submission=submission, org_id=None)

    async with get_session() as s:
        first = await _probe_trials(s, seeded_sweep_task_id)
    assert len(first) == 1, f"Expected 1 probe trial after first sweep, got {len(first)}"

    # Second sweep (same task version): probe should NOT be enqueued again.
    async with get_session() as s:
        await create_task_sweep_core(s, submission=submission, org_id=None)

    async with get_session() as s:
        second = await _probe_trials(s, seeded_sweep_task_id)
    assert len(second) == 1, (
        f"Expected dedup to hold (still 1 probe trial), got {len(second)}"
    )


@pytest.mark.asyncio
async def test_sweep_does_not_probe_by_default(seeded_sweep_task_id):
    """Auto-probe is off by default: a sweep without ``run_probe`` must NOT
    enqueue any probe trial."""
    from oddish.core.endpoints import create_task_sweep_core

    submission = TaskSweepSubmission(
        task_id=seeded_sweep_task_id,
        append_to_task=True,
        configs=[
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=1,
            )
        ],
        user="test",
    )

    async with get_session() as s:
        await create_task_sweep_core(s, submission=submission, org_id=None)

    async with get_session() as s:
        probes = await _probe_trials(s, seeded_sweep_task_id)
    assert len(probes) == 0, (
        f"Expected no probe trials by default, got {len(probes)}"
    )

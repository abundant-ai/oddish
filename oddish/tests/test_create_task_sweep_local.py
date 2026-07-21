"""Test that create_task_sweep_core dispatches to the local runner when LOCAL_MODE=1.

When ``ODDISH_LOCAL_MODE=1``, probe trials submitted via the sweep endpoint
should fire ``run_trial_locally(trial.id, dry_run=False)`` as a background
asyncio task instead of going through the Modal queue. This test sets the env
var, monkeypatches the runner, submits a sweep, and asserts the runner was
called once per new trial.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402

from oddish.schemas import (  # noqa: E402
    AgentModelPair,
    TaskMetadata,
    TaskProvenance,
    TaskSubmission,
    TaskSweepSubmission,
    TrialSpec,
)
from oddish.core.endpoints import create_task_sweep_core  # noqa: E402
from oddish.core import tasks as tasks_api  # noqa: E402
from oddish.db import TaskModel, TaskVersionModel, TrialModel, get_session  # noqa: E402
from oddish.db.models import TaskUploadEventModel  # noqa: E402
from oddish.queue import create_task  # noqa: E402


@pytest_asyncio.fixture
async def seeded_task_id(tmp_path):
    """Insert a TaskModel and return its id. Cleanup after."""
    task_id = "test_sweep_local_task"
    task_dir = tmp_path / "fake-task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("solve")
    (task_dir / "task.toml").write_text('version = "1.0"\n')

    async with get_session() as s:
        s.add(
            TaskModel(
                id=task_id,
                name="sweep-local-test",
                user="test",
                task_path=str(task_dir),
            )
        )
    yield task_id
    async with get_session() as s:
        await s.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
        )
        await s.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))


@pytest.mark.asyncio
async def test_local_mode_dispatches_local_runner(monkeypatch, seeded_task_id):
    """When ODDISH_LOCAL_MODE=1, create_task_sweep_core should call run_trial_locally
    for each new trial via asyncio.create_task (not the Modal enqueue path).
    """
    monkeypatch.setenv("ODDISH_LOCAL_MODE", "1")
    # Reload settings if cached
    import oddish.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "settings", cfg_mod.Settings())

    fake = AsyncMock()
    monkeypatch.setattr("oddish.worker.local_runner.run_trial_locally", fake)

    submission = TaskSweepSubmission(
        task_id=seeded_task_id,
        append_to_task=True,
        configs=[
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=1,
            )
        ],
        user="alice",
        extra_instructions="cheat",
    )

    async with get_session() as s:
        task, new_trials, is_append, exp = await create_task_sweep_core(
            s, submission=submission, org_id=None
        )

    # asyncio.create_task fires fire-and-forget; await one tick so the task runs
    await asyncio.sleep(0.05)

    assert fake.await_count == len(new_trials)
    assert fake.await_count >= 1
    fake.assert_any_await(new_trials[0].id, dry_run=False)


class _FakeSweepStorage:
    """Fakes the S3 round trip ``create_task_sweep_core``'s create-mode
    branch needs (``resolve_task_storage``'s root-archive check)."""

    def __init__(self, task_id: str):
        self._archive_key = f"tasks/{task_id}/.oddish-task.tar.gz"

    async def object_exists(self, key: str) -> bool:
        return key == self._archive_key

    async def prefix_exists(self, prefix: str) -> bool:
        return True

    async def list_keys(self, prefix: str) -> list[str]:
        return [self._archive_key]


def _sweep_metadata() -> TaskMetadata:
    return TaskMetadata(
        description="Carried through the sweep translation.",
        category="ml-training",
        allow_internet=False,
        cpus=4,
        memory_mb=16384,
    )


def _sweep_provenance() -> TaskProvenance:
    return TaskProvenance(
        source_repo="abundant-ai/harbor-lh",
        source_commit="b" * 40,
        source_ref="main",
        uploader_is_ci=True,
        ci_provider="github_actions",
    )


@pytest.mark.asyncio
async def test_sweep_create_mode_carries_metadata_and_provenance(monkeypatch):
    """Finding 1 regression: ``build_task_submission_from_sweep`` must carry
    ``task_metadata``/``provenance`` through, or ``oddish run`` (the most
    common upload path) silently drops both at ``create_task``.
    """
    task_id = "test_sweep_metadata_task"
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: _FakeSweepStorage(task_id))

    submission = TaskSweepSubmission(
        task_id=task_id,
        configs=[AgentModelPair(agent="nop", model=None, n_trials=1)],
        user="tester",
        task_metadata=_sweep_metadata(),
        provenance=_sweep_provenance(),
    )

    try:
        async with get_session() as s:
            task, new_trials, is_append, exp = await create_task_sweep_core(
                s, submission=submission, org_id=None
            )
        assert is_append is False

        async with get_session() as s:
            task_row = (
                await s.execute(select(TaskModel).where(TaskModel.id == task.id))
            ).scalar_one()
            assert task_row.description == "Carried through the sweep translation."
            assert task_row.category == "ml-training"

            version_row = (
                await s.execute(
                    select(TaskVersionModel).where(
                        TaskVersionModel.id == task_row.current_version_id
                    )
                )
            ).scalar_one()
            assert version_row.allow_internet is False
            assert version_row.description_snapshot == (
                "Carried through the sweep translation."
            )

            events = (
                await s.execute(
                    select(TaskUploadEventModel).where(
                        TaskUploadEventModel.task_id == task.id
                    )
                )
            ).scalars().all()
            assert len(events) == 1
            assert events[0].created_version is True
            assert events[0].source_repo == "abundant-ai/harbor-lh"
            assert events[0].source_commit == "b" * 40
            assert events[0].uploader_is_ci is True
    finally:
        async with get_session() as s:
            await s.execute(TrialModel.__table__.delete().where(TrialModel.task_id == task_id))
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))


@pytest.mark.asyncio
async def test_create_task_reuse_branch_applies_descriptive_metadata(monkeypatch):
    """Finding 2 regression: ``create_task``'s ``existing_max is not None``
    branch (reusing a pre-existing version instead of cutting a new one)
    must still apply descriptive metadata -- last-write-wins regardless of
    whether a version was cut, matching ``complete_task_upload``'s sibling
    existing-task branch.

    FK cascade (``task_versions.task_id`` -> ``tasks.id`` ON DELETE CASCADE,
    NOT NULL) makes this branch's real precondition -- a ``task_versions``
    row surviving while its own ``task_id`` has no ``tasks`` row yet --
    impossible to construct via genuine inserts: ``create_task`` always
    inserts a brand-new ``tasks`` row for ``task_id`` before checking for an
    existing version. Redirect just that lookup at a real, already-committed
    version row so the branch's actual logic (skip cutting a version, still
    apply descriptive metadata, still record the event) runs for real
    against real, FK-valid rows.
    """
    seed_submission = TaskSubmission(
        task_path="s3://tasks/reuse-branch-seed/",
        name="reuse-branch-seed",
        user="tester",
        trials=[TrialSpec(agent="nop", model=None)],
    )
    target_task_id = "reuse-branch-target"
    try:
        async with get_session() as s:
            seed_task = await create_task(s, seed_submission, org_id=None)
        async with get_session() as s:
            seed_version = (
                await s.execute(
                    select(TaskVersionModel).where(
                        TaskVersionModel.id == seed_task.current_version_id
                    )
                )
            ).scalar_one()

        changed_metadata = _sweep_metadata()
        target_submission = TaskSubmission(
            task_path="s3://tasks/reuse-branch-target/",
            name="reuse-branch-target",
            user="tester",
            trials=[TrialSpec(agent="nop", model=None)],
            task_metadata=changed_metadata,
            provenance=_sweep_provenance(),
        )

        async with get_session() as s:
            real_scalar = s.scalar
            real_execute = s.execute

            async def fake_scalar(statement, *args, **kwargs):
                if "max(task_versions.version)" in str(statement):
                    return seed_version.version
                return await real_scalar(statement, *args, **kwargs)

            async def fake_execute(statement, *args, **kwargs):
                text = str(statement)
                if "task_versions.version = " in text and "max(" not in text.lower():

                    class _Result:
                        def scalar_one(self_) -> TaskVersionModel:
                            return seed_version

                    return _Result()
                return await real_execute(statement, *args, **kwargs)

            monkeypatch.setattr(s, "scalar", fake_scalar)
            monkeypatch.setattr(s, "execute", fake_execute)

            task = await create_task(
                s, target_submission, task_id=target_task_id, org_id=None
            )

        async with get_session() as s:
            task_row = (
                await s.execute(select(TaskModel).where(TaskModel.id == task.id))
            ).scalar_one()
            assert task_row.description == "Carried through the sweep translation."
            assert task_row.category == "ml-training"

            events = (
                await s.execute(
                    select(TaskUploadEventModel).where(
                        TaskUploadEventModel.task_id == task.id
                    )
                )
            ).scalars().all()
            assert len(events) == 1
            assert events[0].created_version is False
            assert events[0].task_version_id == seed_version.id
    finally:
        async with get_session() as s:
            await s.execute(
                TrialModel.__table__.delete().where(
                    TrialModel.task_id.in_([seed_task.id, target_task_id])
                )
            )
            await s.execute(
                TaskModel.__table__.delete().where(
                    TaskModel.id.in_([seed_task.id, target_task_id])
                )
            )

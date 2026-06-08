"""Tests for ``worker.local_runner``.

These touch the local Postgres database through ``oddish.db.get_session``.
Run with ``.env.local`` sourced so ``ODDISH_DATABASE_URL`` points at the
local stack:

    set -a && source .env.local && set +a && uv run pytest tests/test_local_runner.py -v
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    get_session,
)

from oddish.worker.local_runner import _run_harbor_trial, run_trial_locally


@pytest_asyncio.fixture
async def seeded_trial_id(tmp_path):
    """Insert a minimal Experiment + Task + Trial (status=QUEUED).

    Yields the trial id and cleans up all three rows afterwards. Uses
    short uuid suffixes to avoid clashing with seed rows or other tests.
    """
    suffix = uuid.uuid4().hex[:8]
    experiment_id = f"exp_lr_{suffix}"
    task_id = f"task_lr_{suffix}"
    trial_id = f"trial_lr_{suffix}"

    async with get_session() as session:
        session.add(
            ExperimentModel(
                id=experiment_id,
                name=f"local-runner-test-{suffix}",
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                name=f"local-runner-task-{suffix}",
                user="test",
                task_path=str(tmp_path),
            )
        )
        session.add(
            TrialModel(
                id=trial_id,
                name=f"{task_id}-0",
                task_id=task_id,
                experiment_id=experiment_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-5",
                queue_key="test-local-runner",
                status=TrialStatus.QUEUED,
                origin=TrialOrigin.ODDISH,
            )
        )

    yield trial_id

    async with get_session() as session:
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.id == trial_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )


@pytest.mark.asyncio
async def test_run_trial_locally_dry_run_marks_success(seeded_trial_id):
    """Dry-run path should QUEUED -> RUNNING -> SUCCESS and set timestamps."""
    await run_trial_locally(seeded_trial_id, dry_run=True)

    async with get_session() as session:
        trial = await session.get(TrialModel, seeded_trial_id)
        assert trial is not None
        assert trial.status == TrialStatus.SUCCESS
        assert trial.started_at is not None
        assert trial.finished_at is not None
        assert trial.finished_at >= trial.started_at


@pytest.mark.asyncio
async def test_run_trial_locally_missing_trial_raises():
    with pytest.raises(ValueError, match="not found"):
        await run_trial_locally("nonexistent-trial-id", dry_run=True)


@pytest_asyncio.fixture
async def seeded_probe_trial_with_task_dir(tmp_path):
    """Insert Experiment + Task + Trial backed by a real on-disk task dir.

    The Trial's ``harbor_config`` carries the probe task-mutation overlay
    payload (``mode=probe`` + ``extra_instructions``) so the runner takes
    the overlay code path. Yields ``(trial_id, original_task_dir)`` so the
    test can assert that the original on-disk instruction.md was *not*
    mutated by the runner.
    """
    suffix = uuid.uuid4().hex[:8]
    experiment_id = f"exp_lr_overlay_{suffix}"
    task_id = f"task_lr_overlay_{suffix}"
    trial_id = f"trial_lr_overlay_{suffix}"

    task_dir = tmp_path / "fake-task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("solve the task")
    (task_dir / "task.toml").write_text('version = "1.0"\n')

    async with get_session() as session:
        session.add(
            ExperimentModel(
                id=experiment_id,
                name=f"local-runner-overlay-{suffix}",
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                name=f"local-runner-overlay-task-{suffix}",
                user="test",
                task_path=str(task_dir),
            )
        )
        session.add(
            TrialModel(
                id=trial_id,
                name=f"{task_id}-0",
                task_id=task_id,
                experiment_id=experiment_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-5",
                queue_key="test-local-runner",
                status=TrialStatus.RUNNING,
                origin=TrialOrigin.ODDISH,
                harbor_config={
                    "mode": "probe",
                    "extra_instructions": "be adversarial",
                },
            )
        )

    yield trial_id, task_dir

    async with get_session() as session:
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.id == trial_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )


@pytest.mark.asyncio
async def test_probe_overlay_prepends_extra_instructions_to_instruction_md(
    monkeypatch, seeded_probe_trial_with_task_dir
):
    """Overlay path: copy task to temp dir + prepend operator prompt to instruction.md.

    Asserts:
      - Harbor sees a *temp* path (not the original task dir) so the source
        tree is never mutated.
      - The instruction.md Harbor sees contains both the operator prompt
        and the original task instruction.
      - The original on-disk instruction.md is unchanged after the run.
      - The temp work dir is cleaned up after Harbor exits.
    """
    trial_id, original_task_dir = seeded_probe_trial_with_task_dir
    captured: dict[str, object] = {}

    class FakeTrial:
        """Stand-in for ``harbor.trial.trial.Trial``.

        Captures the path + instruction.md the runner pointed it at. Also
        exposes a ``create`` async classmethod so the runner can use either
        ``Trial(cfg)`` or ``await Trial.create(cfg)``.
        """

        def __init__(self, cfg, **_kwargs):
            captured["task_path"] = Path(cfg.task.path)
            captured["instruction"] = (
                Path(cfg.task.path) / "instruction.md"
            ).read_text()
            self.result = MagicMock()
            self.result.verifier_result = MagicMock(rewards={"reward": 0.0})
            self.result.model_dump = lambda mode=None: {}

        @classmethod
        async def create(cls, cfg):
            return cls(cfg)

        async def run(self):
            return self.result

    monkeypatch.setattr("oddish.worker.local_runner.Trial", FakeTrial)

    await _run_harbor_trial(trial_id)

    # Harbor must see a temp copy, never the original task dir on disk.
    assert captured["task_path"] != original_task_dir
    assert "be adversarial" in captured["instruction"]
    assert "solve the task" in captured["instruction"]
    # System framing must be prepended so the agent treats the operator
    # directive as the goal (not the original task).
    assert "Probe runtime" in captured["instruction"]
    assert "OPERATOR DIRECTIVE" in captured["instruction"]
    # Framing must appear BEFORE the operator directive.
    framing_idx = captured["instruction"].index("Probe runtime")
    op_idx = captured["instruction"].index("be adversarial")
    task_idx = captured["instruction"].index("solve the task")
    assert framing_idx < op_idx < task_idx

    # Original task dir on disk is untouched.
    assert (
        original_task_dir / "instruction.md"
    ).read_text() == "solve the task"

    # Temp work dir is cleaned up after Harbor exits.
    assert not Path(captured["task_path"]).exists()

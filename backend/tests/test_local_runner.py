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
from sqlalchemy import text

from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    get_session,
)
from oddish.db.models import task_experiments

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
        # Settlement resolves the QA shadow experiment through the task's
        # live membership, so seed the row every real flow writes.
        await session.flush()
        await session.execute(
            task_experiments.insert().values(
                task_id=task_id, experiment_id=experiment_id
            )
        )

    yield trial_id

    async with get_session() as session:
        # Settling the trial spawns a QA trial in a shadow experiment, so
        # sweep by task rather than by the ids seeded above.
        await session.execute(
            text("delete from worker_jobs where subject_id like :prefix"),
            {"prefix": f"{task_id}-%"},
        )
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
        )
        await session.execute(
            task_experiments.delete().where(task_experiments.c.task_id == task_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                (ExperimentModel.id == experiment_id)
                | (ExperimentModel.shadow_of == experiment_id)
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
async def test_run_trial_locally_missing_trial_skips():
    """A missing trial is not claimable; the runner returns without raising."""
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
    (task_dir / "solution").mkdir()

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
            captured["hooks"] = []

        @classmethod
        async def create(cls, cfg):
            return cls(cfg)

        def add_hook(self, event, hook):
            # Real ``Trial.add_hook``; the runner registers the probe task-dir
            # upload hook here. Record it so the test can assert it was wired.
            captured["hooks"].append((event, hook))

        async def run(self):
            return self.result

    monkeypatch.setattr("oddish.worker.local_runner.Trial", FakeTrial)

    async def fake_mint_probe_creds(*, org_id, trial_id):
        return "fake-key-id", {
            "ODDISH_API_KEY": "fake-key",
            "ODDISH_API_BASE_URL": "http://localhost:8800",
        }

    monkeypatch.setattr(
        "oddish.worker.local_runner.mint_probe_creds", fake_mint_probe_creds
    )

    await _run_harbor_trial(trial_id)

    # Harbor must see a temp copy, never the original task dir on disk.
    assert captured["task_path"] != original_task_dir
    assert "be adversarial" in captured["instruction"]
    assert "solve the task" in captured["instruction"]
    # The operator directive leads; the original spec follows, relabeled as the
    # *real agent's* brief (not the probe's own task) with a visibility map so
    # the probe doesn't flag its own staged files as vulnerabilities.
    assert "THIS IS THE TASK" not in captured["instruction"]
    assert "REAL AGENT BRIEF" in captured["instruction"]
    assert "WHAT THE REAL AGENT SEES" in captured["instruction"]
    op_idx = captured["instruction"].index("be adversarial")
    task_idx = captured["instruction"].index("solve the task")
    assert op_idx < task_idx

    # Original task dir on disk is untouched.
    assert (original_task_dir / "instruction.md").read_text() == "solve the task"

    # Temp work dir is cleaned up after Harbor exits.
    assert not Path(captured["task_path"]).exists()


@pytest.mark.asyncio
async def test_probe_uploads_cli_to_harness(
    monkeypatch, seeded_probe_trial_with_task_dir
):
    """Only the CLI is uploaded to /probe-harness; no first upload to /opt/oddish-probe."""
    trial_id, _ = seeded_probe_trial_with_task_dir
    uploads: list[tuple[str, str]] = []
    captured: dict[str, object] = {"env": None, "exec_cmds": []}

    class FakeEnv:
        async def upload_dir(self, *, source_dir, target_dir):
            names = sorted(p.name for p in Path(source_dir).iterdir())
            uploads.append((target_dir, ",".join(names)))

        async def exec(self, command, **_kwargs):
            captured["exec_cmds"].append(command)
            from types import SimpleNamespace

            return SimpleNamespace(return_code=0)

    class FakeTrial:
        def __init__(self, cfg, **_):
            captured["env"] = dict(cfg.agent.env or {})
            self.result = MagicMock()
            self.result.verifier_result = MagicMock(rewards={"reward": 0.0})
            self.result.model_dump = lambda mode=None: {}
            self._hooks = []

        @classmethod
        async def create(cls, cfg):
            return cls(cfg)

        def add_hook(self, event, hook):
            self._hooks.append(hook)

        async def run(self):
            event = MagicMock()
            event.environment = FakeEnv()
            for h in self._hooks:
                await h(event)
            return self.result

    monkeypatch.setattr("oddish.worker.local_runner.Trial", FakeTrial)

    async def fake_mint_probe_creds(*, org_id, trial_id):
        return "fake-key-id", {
            "ODDISH_API_KEY": "fake-key",
            "ODDISH_API_BASE_URL": "http://localhost:8800",
        }

    monkeypatch.setattr(
        "oddish.worker.local_runner.mint_probe_creds", fake_mint_probe_creds
    )
    await _run_harbor_trial(trial_id)

    targets = {t for t, _ in uploads}
    # No longer uploads to the hidden stage — task assets removed from first upload.
    assert "/opt/oddish-probe" not in targets
    assert "/probe-harness" in targets
    # The /probe-harness mount carries ONLY the CLI.
    harness = next(names for t, names in uploads if t == "/probe-harness")
    assert harness == "oddish-query"
    # STAGE_DIR is gone; three new env vars take its place.
    assert "ODDISH_PROBE_STAGE_DIR" not in captured["env"]
    assert captured["env"]["ODDISH_PROBE_TASK_ID"]
    assert captured["env"]["ODDISH_PROBE_HARBOR_REPO"]
    assert captured["env"]["ODDISH_PROBE_HARBOR_REF"]
    # CLI executable check was attempted.
    assert any("/probe-harness/oddish-query" in cmd for cmd in captured["exec_cmds"])


@pytest.mark.asyncio
async def test_local_probe_env_contract(monkeypatch, seeded_probe_trial_with_task_dir):
    """Probe agent env: 3 new vars present, ODDISH_PROBE_STAGE_DIR absent."""
    trial_id, _ = seeded_probe_trial_with_task_dir
    captured: dict[str, object] = {"env": None}

    class FakeEnv:
        async def upload_dir(self, *, source_dir, target_dir):
            pass

        async def exec(self, command, **_kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(return_code=0)

    class FakeTrial:
        def __init__(self, cfg, **_):
            captured["env"] = dict(cfg.agent.env or {})
            self.result = MagicMock()
            self.result.verifier_result = MagicMock(rewards={"reward": 0.0})
            self.result.model_dump = lambda mode=None: {}
            self._hooks = []

        @classmethod
        async def create(cls, cfg):
            return cls(cfg)

        def add_hook(self, event, hook):
            self._hooks.append(hook)

        async def run(self):
            event = MagicMock()
            event.environment = FakeEnv()
            for h in self._hooks:
                await h(event)
            return self.result

    monkeypatch.setattr("oddish.worker.local_runner.Trial", FakeTrial)

    async def fake_mint_probe_creds(*, org_id, trial_id):
        return "fake-key-id", {
            "ODDISH_API_KEY": "fake-key",
            "ODDISH_API_BASE_URL": "http://localhost:8800",
        }

    monkeypatch.setattr(
        "oddish.worker.local_runner.mint_probe_creds", fake_mint_probe_creds
    )
    await _run_harbor_trial(trial_id)

    env = captured["env"]
    assert env["ODDISH_PROBE_TASK_ID"]
    assert env["ODDISH_PROBE_HARBOR_REPO"]
    assert env["ODDISH_PROBE_HARBOR_REF"]
    assert "ODDISH_PROBE_STAGE_DIR" not in env

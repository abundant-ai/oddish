"""Local in-process trial runner. Used when ``ODDISH_LOCAL_MODE=1``.

Bypasses the Modal queue and runs trials directly via Harbor's Python
API, talking to a local Docker daemon for the env. State is written to
the same Postgres rows the Modal worker would update, so the rest of
the stack (FE, analysis pipeline) sees a normal trial.

Task 8 wires ``_run_harbor_trial`` to actually invoke Harbor and adds
the freeform task-mutation overlay: when ``harbor_config.extra_instructions``
is set, the runner copies the task dir to a temp work dir, prepends the
operator's prompt to ``instruction.md``, and points Harbor at the temp
copy. Harbor itself stays unpatched -- it just sees a normal task with a
modified ``instruction.md``. Mirrors the long-horizon ``/cheat`` CI
workflow which ``cat``s the cheating prompt into ``instruction.md``
before submitting.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from harbor.trial.trial import Trial

from oddish.db import TaskModel, TrialModel, TrialStatus, get_session

logger = logging.getLogger(__name__)


async def run_trial_locally(trial_id: str, *, dry_run: bool = False) -> None:
    """Execute a freeform trial in-process and mirror status to the DB.

    Status transitions: ``QUEUED`` -> ``RUNNING`` -> ``SUCCESS``
    (or ``FAILED`` on exception, with ``error_message`` populated).

    When ``dry_run`` is True, skips the actual Harbor call. Used in
    tests to exercise the status-transition path without spinning up
    Docker.
    """
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            raise ValueError(f"Trial {trial_id} not found")
        trial.status = TrialStatus.RUNNING
        trial.started_at = datetime.now(timezone.utc)
        logger.info("local_runner: trial %s -> RUNNING", trial_id)

    try:
        if not dry_run:
            await _run_harbor_trial(trial_id)
    except Exception as exc:
        logger.exception("local_runner: trial %s failed", trial_id)
        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id)
            if trial is not None:
                trial.status = TrialStatus.FAILED
                trial.error_message = str(exc)
                trial.finished_at = datetime.now(timezone.utc)
        raise

    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            raise ValueError(
                f"Trial {trial_id} disappeared mid-run; cannot mark SUCCESS"
            )
        trial.status = TrialStatus.SUCCESS
        trial.finished_at = datetime.now(timezone.utc)
        logger.info("local_runner: trial %s -> SUCCESS", trial_id)


async def _run_harbor_trial(trial_id: str) -> None:
    """Execute the trial against a local Harbor instance.

    Reads the trial row + linked task to get ``task_path``, ``agent``,
    ``model`` and ``harbor_config``. If ``harbor_config.extra_instructions``
    is set, copies the task dir to a temp work dir and prepends the
    operator prompt to ``instruction.md`` so Harbor sees the mutated
    instruction without any Harbor-side patch. Persists ``reward`` and
    the full ``TrialResult`` JSON back to the trial row, then cleans up
    the temp work dir.
    """
    from harbor.models.trial.config import (
        AgentConfig,
        TaskConfig,
        TrialConfig,
    )

    # ---------------------------------------------------------------
    # Load trial + task. ``selectin`` would normally hydrate
    # ``trial.task`` eagerly, but we hit it via ``session.get(TaskModel, ...)``
    # explicitly to mirror the canonical pattern used by the Modal trial
    # handler (oddish/workers/queue/trial_handler.py).
    # ---------------------------------------------------------------
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            raise ValueError(f"Trial {trial_id} not found")
        task = await session.get(TaskModel, trial.task_id)
        if task is None:
            raise ValueError(
                f"Trial {trial_id} references missing task {trial.task_id}"
            )
        task_path = Path(task.task_path)
        harbor_config = trial.harbor_config or {}
        agent_name = trial.agent
        model_name = trial.model
        extra_instructions = harbor_config.get("extra_instructions")

    if not task_path.exists():
        raise FileNotFoundError(
            f"Task dir not found for trial {trial_id}: {task_path}"
        )

    # ---------------------------------------------------------------
    # Freeform overlay: copy the task dir to a temp work dir and
    # prepend the operator prompt to ``instruction.md``. Harbor reads
    # the modified file from the copy without any patch.
    # ---------------------------------------------------------------
    work_root: Path | None = None
    if extra_instructions:
        work_root = Path(tempfile.mkdtemp(prefix=f"freeform-{trial_id}-"))
        work_task_dir = work_root / task_path.name
        shutil.copytree(task_path, work_task_dir, symlinks=True)
        instr_path = work_task_dir / "instruction.md"
        original = instr_path.read_text() if instr_path.exists() else ""
        instr_path.write_text(f"{extra_instructions}\n\n---\n\n{original}")
        actual_task_path = work_task_dir
    else:
        actual_task_path = task_path

    # ---------------------------------------------------------------
    # Build Harbor configs and run the trial.
    # ---------------------------------------------------------------
    trials_dir = Path(f"/tmp/oddish-local-trials/{trial_id}")
    trials_dir.mkdir(parents=True, exist_ok=True)

    cfg = TrialConfig(
        task=TaskConfig(path=actual_task_path),
        agent=AgentConfig(name=agent_name, model_name=model_name),
        trials_dir=trials_dir,
    )

    # ``Trial.__init__`` requires a pre-loaded ``Task`` and is marked
    # deprecated; ``Trial.create`` is the supported entrypoint.
    # Tests monkeypatch ``worker.local_runner.Trial`` -- the fake
    # exposes a matching ``create`` classmethod so the call shape stays
    # identical between the real Harbor Trial and the test double.
    harbor_trial = await Trial.create(cfg)
    try:
        await harbor_trial.run()
    finally:
        if work_root is not None:
            shutil.rmtree(work_root, ignore_errors=True)

    # ---------------------------------------------------------------
    # Persist reward + raw result back to the trial row.
    # ---------------------------------------------------------------
    result = harbor_trial.result
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            return
        trial.harbor_result_path = str(trials_dir)
        verifier_result = getattr(result, "verifier_result", None) if result else None
        rewards = getattr(verifier_result, "rewards", None) if verifier_result else None
        if rewards:
            reward_value = rewards.get("reward")
            if reward_value is not None:
                trial.reward = float(reward_value)
        if result is not None and hasattr(result, "model_dump"):
            try:
                trial.result = result.model_dump(mode="json")
            except TypeError:
                # Some MagicMock-style stubs don't accept ``mode``.
                trial.result = result.model_dump()

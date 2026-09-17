"""Pass the worker's own trial record to an opted-in separate verifier."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

import tomllib
from harbor.models.task.config import TaskConfig, VerifierEnvironmentMode
from harbor.models.task.verifier_mode import (
    resolve_effective_verifier_env_config,
    resolve_task_verifier_mode,
)
from harbor.models.trajectories.trajectory import Trajectory
from harbor.models.trial.config import VerifierConfig
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.verifier import Verifier

MAX_TRAJECTORY_BYTES = 20_000_000
_IMPORT_PATH = "oddish.workers.harbor.trusted_trajectory:TrustedTrajectoryVerifier"
_INPUT_PATH = "/logs/verifier/input-trajectory.json"
_HASH_PATH = _INPUT_PATH + ".sha256"


def configure_trusted_trajectory(
    task_path: Path, config: VerifierConfig
) -> VerifierConfig:
    """Choose only a fixed Oddish class; metadata never supplies an import path."""
    with (task_path / "task.toml").open("rb") as source:
        raw = tomllib.load(source)
    metadata = raw.get("metadata", {})
    options = metadata.get("oddish", {}) if isinstance(metadata, dict) else {}
    if (
        not isinstance(options, dict)
        or options.get("verifier_trusted_trajectory") is not True
    ):
        return config
    if options.get("verifier_judge_costs") is not True:
        raise ValueError("Trusted verifier input requires judge cost accounting.")
    if config.disable or config.import_path is not None or config.kwargs:
        raise ValueError(
            "Trusted verifier input requires the standard verifier configuration."
        )
    try:
        task = TaskConfig.model_validate(raw)
    except ValueError:
        raise ValueError(
            "Trusted verifier input has an invalid task configuration."
        ) from None
    environment = resolve_effective_verifier_env_config(task, None)
    if (
        task.steps
        or resolve_task_verifier_mode(task) != VerifierEnvironmentMode.SEPARATE
        or environment is None
        or environment.os.value != "linux"
    ):
        raise ValueError("Trusted verifier input requires one separate Linux verifier.")
    return config.model_copy(update={"import_path": _IMPORT_PATH})


def _read_trajectory(trial_paths: Any) -> bytes:
    """Read only this trial's regular host file, without following a symlink."""
    directory = trial_paths.agent_dir
    source = directory / "trajectory.json"
    if directory.is_symlink() or not directory.resolve().is_relative_to(
        trial_paths.trial_dir.resolve()
    ):
        raise ValueError("The worker trajectory path is invalid.")
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("The worker trajectory must be a regular file.")
            if info.st_size > MAX_TRAJECTORY_BYTES:
                raise ValueError("The worker trajectory exceeds the input limit.")
            body = stream.read(MAX_TRAJECTORY_BYTES + 1)
    except OSError:
        raise ValueError("The worker trajectory is not available.") from None
    if len(body) > MAX_TRAJECTORY_BYTES:
        raise ValueError("The worker trajectory exceeds the input limit.")
    try:
        Trajectory.model_validate_json(body)
    except (ValueError, RecursionError):
        raise ValueError("The worker trajectory is not valid ATIF.") from None
    return body


def _record_input_failure(trial_paths: Any) -> None:
    """Establish zero judge spend only before the standard verifier starts."""
    directory = trial_paths.verifier_dir
    if directory.is_symlink() or not directory.resolve().is_relative_to(
        trial_paths.trial_dir.resolve()
    ):
        raise ValueError("The worker verifier path is invalid.")
    message = "The worker could not supply the trial trajectory. No judge started."
    report = {
        "worker_trajectory": {
            "kind": "programmatic",
            "score": 0,
            "passed": False,
            "feedback": message,
        }
    }
    for name, text in {
        "reward-details.json": json.dumps(report),
        "reward.txt": "0\n",
        "step-judge-error.txt": message + "\n",
    }.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, delete=False
            ) as output:
                temporary = Path(output.name)
                output.write(text)
            # Replace an old file or link; never open a task-supplied destination.
            os.replace(temporary, directory / name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class TrustedTrajectoryVerifier(Verifier):
    """Use the separate image's verifier after adding the worker's saved input."""

    def __init__(self, **kwargs: Any) -> None:
        # The pinned Harbor custom factory omits its skip_tests_upload argument.
        # This class only serves separate verifier images, which own /tests.
        kwargs.pop("skip_tests_upload", None)
        super().__init__(skip_tests_upload=True, **kwargs)
        options = self.task.config.metadata.get("oddish", {})
        if (
            not isinstance(options, dict)
            or options.get("verifier_trusted_trajectory") is not True
            or options.get("verifier_judge_costs") is not True
            or self.step_name is not None
            or self.task.config.steps
            or resolve_task_verifier_mode(self.task.config)
            != VerifierEnvironmentMode.SEPARATE
            or self.environment.os.value != "linux"
        ):
            raise ValueError(
                "Trusted verifier input requires one separate Linux verifier."
            )

    async def verify(self) -> VerifierResult:
        try:
            body = _read_trajectory(self.trial_paths)
            # Harbor has restored artifacts and made /logs/verifier writable.
            # Remove conflicting files and links before uploading the fixed inputs.
            prepared = await self.environment.exec(
                command=(
                    "test ! -L /logs && test ! -L /logs/verifier && "
                    "mkdir -p /logs/verifier && "
                    "rm -f -- /logs/verifier/input-trajectory.json "
                    "/logs/verifier/input-trajectory.json.sha256"
                ),
                user="root",
            )
            if prepared.return_code != 0:
                raise ValueError("The verifier input directory is not available.")
            with tempfile.TemporaryDirectory(prefix="oddish-verifier-input-") as name:
                local = Path(name) / "trajectory.json"
                digest = Path(name) / "trajectory.json.sha256"
                local.write_bytes(body)
                digest.write_text(
                    hashlib.sha256(body).hexdigest() + "\n", encoding="ascii"
                )
                await self.environment.upload_file(
                    source_path=local, target_path=_INPUT_PATH
                )
                await self.environment.upload_file(
                    source_path=digest, target_path=_HASH_PATH
                )
        except Exception:
            # This boundary is before standard verification: no judge has started.
            _record_input_failure(self.trial_paths)
            return VerifierResult(rewards={"reward": 0})
        return await super().verify()

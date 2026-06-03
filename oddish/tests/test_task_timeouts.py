from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType

from oddish.cli.run import _default_cloud_environment_for_task
from oddish.schemas import TaskSweepSubmission, TrialSpec
from oddish.task_timeouts import (
    TaskTimeoutValidationError,
    validate_task_timeout_config,
)


def test_validate_task_timeout_config_accepts_explicit_timeouts(tmp_path):
    (tmp_path / "task.toml").write_text(
        """
[agent]
timeout_sec = 1800

[verifier]
timeout_sec = 300

[environment]
build_timeout_sec = 300
""".strip()
    )

    validate_task_timeout_config(tmp_path)


def test_validate_task_timeout_config_accepts_multistage_verifier_timeouts(tmp_path):
    (tmp_path / "task.toml").write_text(
        """
[agent]
timeout_sec = 1800

[[verifiers]]
name = "correctness"
type = "shell"
timeout_sec = 300

[[verifiers]]
name = "ux"
type = "cua"
timeout_sec = 600

[environment]
build_timeout_sec = 300
""".strip()
    )

    validate_task_timeout_config(tmp_path)


def test_validate_task_timeout_config_requires_all_explicit_timeouts(tmp_path):
    (tmp_path / "task.toml").write_text(
        """
[agent]
timeout_sec = 1800
""".strip()
    )

    with pytest.raises(TaskTimeoutValidationError, match=r"\[verifier\]\.timeout_sec"):
        validate_task_timeout_config(tmp_path)


def test_validate_task_timeout_config_requires_each_multistage_timeout(tmp_path):
    (tmp_path / "task.toml").write_text(
        """
[agent]
timeout_sec = 1800

[[verifiers]]
name = "correctness"
type = "shell"
timeout_sec = 300

[[verifiers]]
name = "ux"
type = "cua"

[environment]
build_timeout_sec = 300
""".strip()
    )

    with pytest.raises(
        TaskTimeoutValidationError, match=r"\[\[verifiers\]\]\[1\]\.timeout_sec"
    ):
        validate_task_timeout_config(tmp_path)


def test_trial_spec_rejects_timeout_minutes_override():
    with pytest.raises(ValueError, match="timeout_minutes is no longer supported"):
        TrialSpec(agent="codex", model="gpt-5", timeout_minutes=30)


def test_task_sweep_submission_rejects_timeout_minutes_override():
    with pytest.raises(ValueError, match="timeout_minutes is no longer supported"):
        TaskSweepSubmission(
            task_id="task-123",
            user="rishi",
            timeout_minutes=30,
            configs=[{"agent": "codex", "model": "gpt-5"}],
        )


def test_default_cloud_environment_uses_daytona_for_cpu_only_task(tmp_path):
    (tmp_path / "task.toml").write_text(
        """
[agent]
timeout_sec = 1800

[verifier]
timeout_sec = 300

[environment]
build_timeout_sec = 300
gpus = 0
""".strip()
    )

    assert (
        _default_cloud_environment_for_task(tmp_path, override_gpus=None)
        == EnvironmentType.DAYTONA
    )


def test_default_cloud_environment_uses_modal_for_gpu_task(tmp_path):
    (tmp_path / "task.toml").write_text(
        """
[agent]
timeout_sec = 1800

[verifier]
timeout_sec = 300

[environment]
build_timeout_sec = 300
gpus = 1
""".strip()
    )

    assert (
        _default_cloud_environment_for_task(tmp_path, override_gpus=None)
        == EnvironmentType.MODAL
    )


def test_default_cloud_environment_honors_gpu_override(tmp_path):
    assert (
        _default_cloud_environment_for_task(tmp_path, override_gpus=1)
        == EnvironmentType.MODAL
    )
    assert (
        _default_cloud_environment_for_task(tmp_path, override_gpus=0)
        == EnvironmentType.DAYTONA
    )

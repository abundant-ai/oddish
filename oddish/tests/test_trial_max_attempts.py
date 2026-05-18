from __future__ import annotations

from pathlib import Path
import sys

import pytest
import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import api as cli_api
from oddish.cli.api import load_sweep_config, submit_sweep
from oddish.core.sweeps import build_task_submission_from_sweep
from oddish.schemas import TaskSubmission, TaskSweepSubmission, TrialSpec


def test_task_submission_preserves_default_trial_attempts():
    submission = TaskSubmission(
        task_path="s3://tasks/task-123",
        trials=[TrialSpec(agent="codex", model="gpt-5")],
    )

    assert submission.max_trial_attempts == 6


def test_task_sweep_submission_accepts_top_level_max_trial_attempts():
    submission = TaskSweepSubmission(
        task_id="task-123",
        max_trial_attempts=3,
        configs=[{"agent": "codex", "model": "gpt-5", "n_trials": 1}],
    )

    expanded = build_task_submission_from_sweep(
        submission,
        task_path="s3://tasks/task-123",
        trials=[TrialSpec(agent="codex", model="gpt-5")],
    )

    assert expanded.max_trial_attempts == 3


def test_task_sweep_submission_rejects_non_positive_max_trial_attempts():
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        TaskSweepSubmission(
            task_id="task-123",
            max_trial_attempts=0,
            configs=[{"agent": "codex", "model": "gpt-5"}],
        )


def test_load_sweep_config_preserves_top_level_max_trial_attempts(tmp_path):
    config_path = tmp_path / "sweep.yaml"
    config_path.write_text(
        """
agents:
  - name: codex
    model_name: gpt-5
    n_trials: 2
max_trial_attempts: 3
""".strip()
    )

    config = load_sweep_config(config_path)

    assert config["max_trial_attempts"] == 3


def test_load_sweep_config_rejects_invalid_max_trial_attempts(tmp_path):
    config_path = tmp_path / "sweep.yaml"
    config_path.write_text(
        """
agents:
  - name: codex
    model_name: gpt-5
max_trial_attempts: 0
""".strip()
    )

    with pytest.raises(typer.Exit):
        load_sweep_config(config_path)


def test_load_sweep_config_rejects_old_max_attempts_key(tmp_path):
    config_path = tmp_path / "sweep.yaml"
    config_path.write_text(
        """
agents:
  - name: codex
    model_name: gpt-5
max_attempts: 3
""".strip()
    )

    with pytest.raises(typer.Exit):
        load_sweep_config(config_path)


def test_submit_sweep_includes_max_trial_attempts_only_when_overridden(monkeypatch):
    captured: list[dict] = []

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"id": "task-123", "trials_count": 1}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, _url, json):
            captured.append(json)
            return _Response()

    monkeypatch.setattr(cli_api, "get_auth_headers", lambda: {})
    monkeypatch.setattr(cli_api.httpx, "Client", _Client)

    base_kwargs = {
        "api_url": "https://oddish.example",
        "task_id": "task-123",
        "configs": [{"agent": "codex", "model": "gpt-5", "n_trials": 1}],
        "environment": None,
        "user": None,
        "priority": "low",
        "experiment_id": "exp-123",
    }
    submit_sweep(**base_kwargs)
    submit_sweep(**base_kwargs, max_trial_attempts=3)

    assert "max_trial_attempts" not in captured[0]
    assert captured[1]["max_trial_attempts"] == 3

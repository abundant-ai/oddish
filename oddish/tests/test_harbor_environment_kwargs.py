from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType

from oddish.cli import api as cli_api
from oddish.core.sweeps import (
    build_task_submission_from_sweep,
    build_trial_specs_from_sweep,
)
from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSweepSubmission
from oddish.workers import harbor_runner


AGENT_TOOLS_IMAGE = "ghcr.io/org/harbor-agent-tools:tag"


class _SubmittingClient:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, _url: str, *, json: dict) -> httpx.Response:
        self.payloads.append(json)
        return httpx.Response(200, json={"task_id": "task-123"})


def test_submit_sweep_includes_harbor_environment_kwargs(monkeypatch) -> None:
    payloads: list[dict] = []

    def fake_client(*_args: object, **_kwargs: object) -> _SubmittingClient:
        return _SubmittingClient(payloads)

    monkeypatch.setattr(cli_api.httpx, "Client", fake_client)
    monkeypatch.setattr(cli_api, "get_auth_headers", lambda: {})

    cli_api.submit_sweep(
        api_url="https://api.example",
        task_id="task-123",
        configs=[{"agent": "codex", "model": "gpt-5", "n_trials": 1}],
        environment=EnvironmentType.MODAL,
        user=None,
        priority="low",
        experiment_id=None,
        harbor_config={
            "environment": {
                "kwargs": {
                    "agent_tools_image": "ghcr.io/org/old-tools:tag",
                    "keep": "value",
                }
            }
        },
        environment_kwargs=[
            f"agent_tools_image={AGENT_TOOLS_IMAGE}",
            "extra=value",
        ],
        override_cpus=4,
    )

    assert payloads[0]["harbor"]["environment"] == {
        "kwargs": {
            "agent_tools_image": AGENT_TOOLS_IMAGE,
            "keep": "value",
            "extra": "value",
        },
        "override_cpus": 4,
    }


def test_sweep_config_loader_preserves_raw_harbor_block(tmp_path: Path) -> None:
    config_path = tmp_path / "sweep.yaml"
    config_path.write_text(
        f"""
agents:
  - name: codex
    model_name: gpt-5
harbor:
  environment:
    kwargs:
      agent_tools_image: {AGENT_TOOLS_IMAGE}
""".strip()
    )

    config = cli_api.load_sweep_config(config_path)

    assert config["harbor"]["environment"]["kwargs"]["agent_tools_image"] == (
        AGENT_TOOLS_IMAGE
    )


def test_harbor_environment_kwargs_survive_trial_config_round_trip() -> None:
    submission = TaskSweepSubmission(
        task_id="task-123",
        configs=[{"agent": "codex", "model": "gpt-5"}],
        harbor={
            "environment": {
                "kwargs": {
                    "agent_tools_image": AGENT_TOOLS_IMAGE,
                }
            }
        },
    )

    trials = build_trial_specs_from_sweep(submission)
    task_submission = build_task_submission_from_sweep(
        submission,
        task_path="/tmp/task",
        trials=trials,
    )
    harbor_config = _build_harbor_config_for_trial(task_submission, trials[0])

    assert harbor_config is not None
    assert harbor_config["environment"]["kwargs"]["agent_tools_image"] == (
        AGENT_TOOLS_IMAGE
    )


def test_harbor_runner_passes_environment_kwargs_to_job_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    task_path = tmp_path / "task"
    task_path.mkdir()
    jobs_dir = tmp_path / "jobs"
    seen: dict[str, dict] = {}

    class _FakeJob:
        def __init__(self, config: dict):
            self.job_dir = config["jobs_dir"] / "job-1"
            seen["environment_kwargs"] = config["environment"].kwargs

        @classmethod
        async def create(cls, config: dict):
            return cls(config)

        async def run(self):
            self.job_dir.mkdir(parents=True, exist_ok=True)
            (self.job_dir / "result.json").write_text("{}\n", encoding="utf-8")
            return object()

    monkeypatch.setattr(harbor_runner, "validate_task_timeout_config", lambda path: None)
    monkeypatch.setattr(harbor_runner, "_build_agent_config", lambda **kwargs: object())
    monkeypatch.setattr(harbor_runner, "TaskConfig", lambda path: path)
    monkeypatch.setattr(harbor_runner, "JobConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(harbor_runner, "Job", _FakeJob)
    monkeypatch.setattr(
        harbor_runner,
        "_extract_outcome_from_job_result",
        lambda **kwargs: harbor_runner.HarborOutcome(
            reward=1.0,
            error=None,
            exit_code=0,
            duration_sec=kwargs["duration_sec"],
            job_result_path=kwargs["job_result_path"],
            job_dir=kwargs["job_dir"],
        ),
    )

    outcome = asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=jobs_dir,
            environment=EnvironmentType.MODAL,
            harbor_config={
                "environment": {
                    "kwargs": {
                        "agent_tools_image": AGENT_TOOLS_IMAGE,
                    }
                }
            },
        )
    )

    assert outcome.error is None
    assert seen["environment_kwargs"]["agent_tools_image"] == AGENT_TOOLS_IMAGE

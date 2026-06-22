from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType

from oddish.config import settings
from oddish.workers import harbor_runner

AGENT_TOOLS_IMAGE = "ghcr.io/org/harbor-agent-tools:tag"


def _run_and_capture_env_kwargs(environment: EnvironmentType, tmp_path: Path) -> dict:
    """Run run_harbor_trial_async with Harbor's Job faked out, returning the
    environment.kwargs that reached JobConfig."""
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

    import pytest

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            harbor_runner, "validate_task_timeout_config", lambda path: None
        )
        monkeypatch.setattr(
            harbor_runner, "_build_agent_config", lambda **kwargs: object()
        )
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
        asyncio.run(
            harbor_runner.run_harbor_trial_async(
                task_path=task_path,
                agent="nop",
                jobs_dir=jobs_dir,
                environment=environment,
                harbor_config={"environment": {"kwargs": {"keep": "value"}}},
            )
        )
    finally:
        monkeypatch.undo()
    return seen["environment_kwargs"]


def test_modal_env_kwargs_unchanged_passthrough(tmp_path: Path) -> None:
    # On Modal the kwargs are passed through untouched (behavior-preserving).
    kwargs = _run_and_capture_env_kwargs(EnvironmentType.MODAL, tmp_path)
    assert kwargs == {"keep": "value"}


def test_daytona_env_kwargs_get_autostop_defaults(tmp_path: Path) -> None:
    kwargs = _run_and_capture_env_kwargs(EnvironmentType.DAYTONA, tmp_path)
    assert kwargs["keep"] == "value"
    assert kwargs["auto_stop_interval_mins"] == settings.daytona_auto_stop_interval_mins
    assert (
        kwargs["auto_delete_interval_mins"]
        == settings.daytona_auto_delete_interval_mins
    )
    assert kwargs["ephemeral"] == settings.daytona_ephemeral

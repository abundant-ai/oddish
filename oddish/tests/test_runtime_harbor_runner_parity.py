from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType
from harbor.models.trial.config import EnvironmentConfig

from oddish.config import settings
from oddish.runtime.ec2_policy import (
    AWS_ACCOUNT_ID_TAG_KEY,
    DEPLOYMENT_TAG_KEY,
    MANAGED_TAG_KEY,
    TRIAL_ID_TAG_KEY,
    WORKER_JOB_ID_TAG_KEY,
)
from oddish.workers.harbor import runner as harbor_runner


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
    # Modal keeps caller kwargs and adds Oddish's hosted sandbox invariants.
    kwargs = _run_and_capture_env_kwargs(EnvironmentType.MODAL, tmp_path)
    assert kwargs == {
        "keep": "value",
        "app_name": "oddish-trials",
        "modal_exact_gpu_type": True,
    }


def test_daytona_env_kwargs_get_autostop_defaults(tmp_path: Path) -> None:
    kwargs = _run_and_capture_env_kwargs(EnvironmentType.DAYTONA, tmp_path)
    assert kwargs["keep"] == "value"
    assert kwargs["auto_stop_interval_mins"] == settings.daytona_auto_stop_interval_mins
    assert (
        kwargs["auto_delete_interval_mins"]
        == settings.daytona_auto_delete_interval_mins
    )
    assert kwargs["ephemeral"] == settings.daytona_ephemeral


def _capture_resolved_config(
    monkeypatch,
    tmp_path: Path,
    *,
    environment: EnvironmentType,
    ephemeral: bool,
    backend=None,
) -> tuple[dict, Path | None]:
    task_path = tmp_path / ("ephemeral-task" if ephemeral else "normal-task")
    task_path.mkdir()
    captured: dict[str, dict] = {}

    if backend is not None:
        monkeypatch.setattr(
            harbor_runner,
            "get_backend",
            lambda name: backend if name == environment.value else None,
        )
    monkeypatch.setattr(
        harbor_runner, "validate_task_timeout_config", lambda _path: None
    )

    if ephemeral:
        from oddish.workers.harbor import ephemeral as harbor_ephemeral

        async def fake_ephemeral(**kwargs):
            config = kwargs["environment_config"]
            captured["config"] = config.model_dump(mode="json")
            key_path = config.kwargs.get("ssh_key_path")
            if key_path:
                path = Path(key_path)
                assert path.exists()
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
            return harbor_runner.HarborOutcome(
                reward=1.0,
                error=None,
                exit_code=0,
                duration_sec=0.1,
                job_result_path=None,
                job_dir=None,
            )

        monkeypatch.setattr(
            harbor_ephemeral, "run_ephemeral_harbor_trial", fake_ephemeral
        )
    else:
        class FakeJob:
            def __init__(self, config):
                self.job_dir = config["jobs_dir"] / "job-1"
                captured["config"] = config["environment"].model_dump(mode="json")

            @classmethod
            async def create(cls, config):
                return cls(config)

            async def run(self):
                self.job_dir.mkdir(parents=True, exist_ok=True)
                (self.job_dir / "result.json").write_text("{}\n")
                return object()

        monkeypatch.setattr(
            harbor_runner, "_build_agent_config", lambda **_kwargs: object()
        )
        monkeypatch.setattr(harbor_runner, "TaskConfig", lambda path: path)
        monkeypatch.setattr(harbor_runner, "JobConfig", lambda **kwargs: kwargs)
        monkeypatch.setattr(harbor_runner, "Job", FakeJob)
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

    harbor_config = {
        "environment": {"kwargs": {"keep": "value", "tags": {"team": "evals"}}}
    }
    if ephemeral:
        harbor_config.update(
            {
                "variant_id": "ephemeral",
                "source": "https://github.com/dot-ai/harbor",
                "resolved_sha": "a" * 40,
            }
        )
    asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=tmp_path / ("ephemeral-jobs" if ephemeral else "normal-jobs"),
            environment=environment,
            trial_id="trial-123",
            worker_job_id="job-456",
            harbor_config=harbor_config,
        )
    )
    key_path = getattr(backend, "key_path", None)
    return captured["config"], key_path


class _FakeEc2Backend:
    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path

    def harbor_env_kwargs(self, base_kwargs: dict) -> dict:
        self.key_path.write_text("private key\n")
        self.key_path.chmod(0o600)
        return {
            **base_kwargs,
            "region": "us-east-1",
            "ami_id": "ami-123",
            "ssh_key_path": str(self.key_path),
            "tags": {
                **base_kwargs.get("tags", {}),
                MANAGED_TAG_KEY: "true",
                DEPLOYMENT_TAG_KEY: "oddish-test",
                AWS_ACCOUNT_ID_TAG_KEY: "123456789012",
            },
        }

    def acquire_worker_credentials(self, *, include_ssh: bool) -> None:
        assert include_ssh is True

    def release_worker_credentials(self) -> None:
        self.key_path.unlink(missing_ok=True)

    def remove_materialized_worker_credentials(self) -> None:
        self.key_path.unlink(missing_ok=True)


def test_ec2_normal_and_ephemeral_receive_identical_resolved_config_and_tags(
    monkeypatch, tmp_path: Path
) -> None:
    shared_key_path = tmp_path / "ec2-key"
    normal_backend = _FakeEc2Backend(shared_key_path)
    normal, normal_key = _capture_resolved_config(
        monkeypatch,
        tmp_path,
        environment=EnvironmentType.EC2,
        ephemeral=False,
        backend=normal_backend,
    )
    ephemeral_backend = _FakeEc2Backend(shared_key_path)
    ephemeral, ephemeral_key = _capture_resolved_config(
        monkeypatch,
        tmp_path,
        environment=EnvironmentType.EC2,
        ephemeral=True,
        backend=ephemeral_backend,
    )

    assert normal == ephemeral
    assert normal["kwargs"]["tags"] == {
        "team": "evals",
        MANAGED_TAG_KEY: "true",
        DEPLOYMENT_TAG_KEY: "oddish-test",
        AWS_ACCOUNT_ID_TAG_KEY: "123456789012",
        TRIAL_ID_TAG_KEY: "trial-123",
        WORKER_JOB_ID_TAG_KEY: "job-456",
    }
    assert normal_key is not None and not normal_key.exists()
    assert ephemeral_key is not None and not ephemeral_key.exists()


def test_daytona_normal_and_ephemeral_resolved_config_remains_identical(
    monkeypatch, tmp_path: Path
) -> None:
    normal, _ = _capture_resolved_config(
        monkeypatch,
        tmp_path,
        environment=EnvironmentType.DAYTONA,
        ephemeral=False,
    )
    ephemeral, _ = _capture_resolved_config(
        monkeypatch,
        tmp_path,
        environment=EnvironmentType.DAYTONA,
        ephemeral=True,
    )

    assert normal == ephemeral
    assert normal["kwargs"]["keep"] == "value"
    assert normal["kwargs"]["auto_stop_interval_mins"] == (
        settings.daytona_auto_stop_interval_mins
    )
    assert normal["kwargs"]["auto_delete_interval_mins"] == (
        settings.daytona_auto_delete_interval_mins
    )
    assert normal["kwargs"]["ephemeral"] == settings.daytona_ephemeral

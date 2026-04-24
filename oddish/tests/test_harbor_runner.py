from __future__ import annotations

import asyncio
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers import harbor_runner  # noqa: E402
from oddish.workers.queue import trial_handler  # noqa: E402

_DISK_USAGE = namedtuple("DiskUsage", ["total", "used", "free"])


def test_check_local_storage_preflight_reports_low_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harbor_runner.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_runner.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=9, free=1),
    )
    monkeypatch.setattr(
        harbor_runner.os,
        "statvfs",
        lambda path: SimpleNamespace(f_favail=10_000, f_ffree=10_000),
    )

    error = harbor_runner._check_local_storage_preflight(
        tmp_path / "harbor",
        include_temp_root=True,
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is not None
    assert "Insufficient local storage" in error
    assert "minimum 5.0GB required" in error


def test_check_local_storage_preflight_reports_low_inodes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harbor_runner.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_runner.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=1, free=6 * 1024**3),
    )
    monkeypatch.setattr(
        harbor_runner.os,
        "statvfs",
        lambda path: SimpleNamespace(f_favail=12, f_ffree=12),
    )

    error = harbor_runner._check_local_storage_preflight(
        tmp_path / "harbor",
        include_temp_root=True,
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is not None
    assert "inodes" in error
    assert "minimum 1024 required" in error


def test_check_local_storage_preflight_reports_probe_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harbor_runner.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_runner.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=1, free=6 * 1024**3),
    )
    monkeypatch.setattr(
        harbor_runner.os,
        "statvfs",
        lambda path: SimpleNamespace(f_favail=10_000, f_ffree=10_000),
    )

    real_write_text = Path.write_text

    def _fail_probe_write(self: Path, *args, **kwargs):
        if self.name == "probe.txt":
            raise OSError(28, "No space left on device")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _fail_probe_write)

    error = harbor_runner._check_local_storage_preflight(
        tmp_path / "harbor",
        include_temp_root=True,
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is not None
    assert "probe failed" in error
    assert "No space left on device" in error


def test_check_local_storage_preflight_skips_temp_root_when_not_requested(
    monkeypatch, tmp_path
):
    jobs_dir = tmp_path / "harbor"
    temp_root = tmp_path / "tmp"
    seen_paths: list[Path] = []

    def _record_probe(path: Path, **_: object) -> None:
        seen_paths.append(path)
        return None

    monkeypatch.setattr(harbor_runner.tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setattr(harbor_runner, "_probe_storage_root", _record_probe)

    error = harbor_runner._check_local_storage_preflight(
        jobs_dir,
        include_temp_root=False,
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is None
    assert seen_paths == [jobs_dir.resolve()]


def test_run_harbor_trial_async_skips_temp_root_preflight_without_task_patch(
    monkeypatch, tmp_path
):
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"
    seen: dict[str, bool] = {}

    def _fake_preflight(path: Path, *, include_temp_root: bool, **_: object) -> None:
        assert path == jobs_dir
        seen["include_temp_root"] = include_temp_root
        return None

    class _FakeJob:
        def __init__(self, config):
            self.job_dir = config["jobs_dir"] / "job-1"

        @classmethod
        async def create(cls, config):
            return cls(config)

        async def run(self):
            self.job_dir.mkdir(parents=True, exist_ok=True)
            (self.job_dir / "result.json").write_text("{}\n", encoding="utf-8")
            return object()

    monkeypatch.setattr(
        harbor_runner, "_check_local_storage_preflight", _fake_preflight
    )
    monkeypatch.setattr(
        harbor_runner, "validate_task_timeout_config", lambda path: None
    )
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
        )
    )

    assert seen["include_temp_root"] is False
    assert outcome.error is None
    assert outcome.job_result_path is not None


def test_run_harbor_trial_async_checks_temp_root_when_task_patch_needed(
    monkeypatch, tmp_path
):
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("", encoding="utf-8")
    calls: list[bool] = []

    def _fake_preflight(
        path: Path, *, include_temp_root: bool, **_: object
    ) -> str | None:
        calls.append(include_temp_root)
        return "temp root unavailable" if include_temp_root else None

    monkeypatch.setattr(
        harbor_runner, "_check_local_storage_preflight", _fake_preflight
    )
    monkeypatch.setattr(
        harbor_runner, "validate_task_timeout_config", lambda path: None
    )

    outcome = asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=tmp_path / "jobs",
            harbor_config={"docker_image": "ghcr.io/example/image:latest"},
        )
    )

    assert calls == [True]
    assert outcome.error == "temp root unavailable"
    assert outcome.job_dir is None


def test_cleanup_uploaded_job_dir_prunes_empty_parent(monkeypatch, tmp_path):
    base_dir = tmp_path / "harbor"
    job_dir = base_dir / "task-demo.nop.trial-demo" / "20260422-000000"
    job_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text("{}\n")

    monkeypatch.setattr(trial_handler.settings, "harbor_jobs_dir", str(base_dir))

    trial_handler._cleanup_uploaded_job_dir(job_dir, "trial-demo")

    assert base_dir.exists()
    assert not job_dir.exists()
    assert not job_dir.parent.exists()

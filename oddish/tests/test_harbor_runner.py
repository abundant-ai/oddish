from __future__ import annotations

from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

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
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is not None
    assert "probe failed" in error
    assert "No space left on device" in error


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

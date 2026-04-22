"""Tests for retrying Harbor artifact uploads in ``trial_handler``."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue import trial_handler  # noqa: E402


class _FakeStorage:
    def __init__(self, results: list[str | Exception]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, Path]] = []

    async def upload_trial_results(self, trial_id: str, job_dir: Path) -> str:
        self.calls.append((trial_id, job_dir))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_upload_trial_results_retries_then_cleans_up(monkeypatch, tmp_path):
    storage = _FakeStorage(
        [RuntimeError("transient"), "tasks/task-123/trials/trial-123/"]
    )
    cleanup_calls: list[tuple[Path, str]] = []
    sleep_calls: list[float] = []

    monkeypatch.setattr(trial_handler, "get_storage_client", lambda: storage)
    monkeypatch.setattr(
        trial_handler,
        "_cleanup_uploaded_job_dir",
        lambda job_dir, trial_id: cleanup_calls.append((job_dir, trial_id)),
    )

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(trial_handler.asyncio, "sleep", _fake_sleep)

    job_dir = tmp_path / "job"
    job_dir.mkdir()

    s3_key = await trial_handler._upload_trial_results_with_retry(
        trial_id="trial-123",
        job_dir=job_dir,
    )

    assert s3_key == "tasks/task-123/trials/trial-123/"
    assert storage.calls == [("trial-123", job_dir), ("trial-123", job_dir)]
    assert cleanup_calls == [(job_dir, "trial-123")]
    assert sleep_calls == [1.0]


@pytest.mark.asyncio
async def test_upload_trial_results_keeps_local_artifacts_after_final_failure(
    monkeypatch, tmp_path
):
    storage = _FakeStorage(
        [
            RuntimeError("transient-1"),
            RuntimeError("transient-2"),
            RuntimeError("permanent"),
        ]
    )
    cleanup_calls: list[tuple[Path, str]] = []
    sleep_calls: list[float] = []

    monkeypatch.setattr(trial_handler, "get_storage_client", lambda: storage)
    monkeypatch.setattr(
        trial_handler,
        "_cleanup_uploaded_job_dir",
        lambda job_dir, trial_id: cleanup_calls.append((job_dir, trial_id)),
    )

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(trial_handler.asyncio, "sleep", _fake_sleep)

    job_dir = tmp_path / "job"
    job_dir.mkdir()

    s3_key = await trial_handler._upload_trial_results_with_retry(
        trial_id="trial-123",
        job_dir=job_dir,
    )

    assert s3_key is None
    assert storage.calls == [
        ("trial-123", job_dir),
        ("trial-123", job_dir),
        ("trial-123", job_dir),
    ]
    assert cleanup_calls == []
    assert sleep_calls == [1.0, 2.0]

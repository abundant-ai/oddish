from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from oddish.core import trial_io


class _Storage:
    def __init__(self, objects: dict[str, str], listed: list[str] | None = None):
        self.objects = objects
        self.listed = listed or []
        self.list_calls = 0

    async def download_text(self, key: str) -> str:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    async def object_exists(self, key: str) -> bool:
        return key in self.objects

    async def list_keys(self, _prefix: str) -> list[str]:
        self.list_calls += 1
        return self.listed


def _trial(*, prefix: str, attempts: int = 1):
    return SimpleNamespace(
        id="task-1-7",
        name="display-name-not-used-for-current-layout",
        model="openai/gpt-5.5",
        trial_s3_key=prefix,
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc),
        attempts=attempts,
    )


def _clear_cache() -> None:
    trial_io._TRAJECTORY_CACHE.clear()
    trial_io._TRAJECTORY_LOCKS.clear()


def test_manifest_selects_the_exact_sanitized_harbor_directory(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    exact_key = f"{prefix}harbor=25run/agent/trajectory.json"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "harbor%run"}]}
            ),
            exact_key: json.dumps({"trial_name": "current-attempt"}),
            f"{prefix}old-run/agent/trajectory.json": json.dumps(
                {"trial_name": "old-attempt"}
            ),
        },
        listed=[f"{prefix}old-run/agent/trajectory.json"],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_trajectory(_trial(prefix=prefix, attempts=2))
    )

    assert result == {"trial_name": "current-attempt"}
    assert storage.list_calls == 0


def test_manifest_missing_trajectory_never_falls_back_to_an_old_retry(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-3/"
    old_key = f"{prefix}old-run/agent/trajectory.json"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            old_key: json.dumps({"trial_name": "old-attempt"}),
        },
        listed=[old_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_trajectory(_trial(prefix=prefix, attempts=3))
    )

    assert result is None
    assert storage.list_calls == 0


def test_manifestless_historical_layout_uses_deterministic_fallback(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/"
    first = f"{prefix}a-run/agent/trajectory.json"
    second = f"{prefix}z-run/agent/trajectory.json"
    storage = _Storage(
        {
            first: json.dumps({"trial_name": "deterministic-first"}),
            second: json.dumps({"trial_name": "later"}),
        },
        listed=[second, first],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(trial_io.read_trial_trajectory(_trial(prefix=prefix)))

    assert result == {"trial_name": "deterministic-first"}
    assert storage.list_calls == 1


def test_manifest_existence_error_never_activates_historical_fallback(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-3/"
    old_key = f"{prefix}old-run/agent/trajectory.json"
    storage = _Storage(
        {old_key: json.dumps({"trial_name": "old-attempt"})},
        listed=[old_key],
    )

    async def fail_manifest_check(_key: str) -> bool:
        raise RuntimeError("S3 temporarily unavailable")

    storage.object_exists = fail_manifest_check
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    try:
        asyncio.run(trial_io.read_trial_trajectory(_trial(prefix=prefix, attempts=3)))
    except RuntimeError as exc:
        assert str(exc) == "S3 temporarily unavailable"
    else:
        raise AssertionError("manifest lookup error should propagate")
    assert storage.list_calls == 0


def test_retry_changes_the_cache_identity(monkeypatch):
    _clear_cache()
    first_prefix = "tasks/task-1/trials/task-1-7/attempt-1/"
    second_prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    storage = _Storage(
        {
            f"{first_prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "run-1"}]}
            ),
            f"{first_prefix}run-1/agent/trajectory.json": json.dumps(
                {"trial_name": "attempt-1"}
            ),
            f"{second_prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "run-2"}]}
            ),
            f"{second_prefix}run-2/agent/trajectory.json": json.dumps(
                {"trial_name": "attempt-2"}
            ),
        }
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = _trial(prefix=first_prefix, attempts=1)

    first = asyncio.run(trial_io.read_trial_trajectory(trial))
    trial.attempts = 2
    trial.trial_s3_key = second_prefix
    second = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert first == {"trial_name": "attempt-1"}
    assert second == {"trial_name": "attempt-2"}

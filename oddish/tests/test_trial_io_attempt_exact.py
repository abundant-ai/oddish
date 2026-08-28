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
        self.download_calls: list[str] = []

    async def download_text(self, key: str) -> str:
        self.download_calls.append(key)
        try:
            return self.objects[key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    async def download_bytes(self, key: str) -> bytes:
        return (await self.download_text(key)).encode()

    async def object_exists(self, key: str) -> bool:
        return key in self.objects

    async def list_keys(self, _prefix: str) -> list[str]:
        self.list_calls += 1
        return self.listed


def _trial(*, prefix: str | None, attempts: int = 1):
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


def test_summary_inputs_share_the_manifest_selected_directory(monkeypatch):
    prefix = "tasks/task-1/trials/task-1-7/attempt-4/"
    current_prefix = f"{prefix}current=25run/"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current%run"}]}
            ),
            f"{current_prefix}agent/trajectory.json": json.dumps(
                {"trial_name": "current-attempt"}
            ),
            f"{current_prefix}task/instruction.md": "repair the broker",
            f"{current_prefix}verifier/test-stdout.txt": "PASS\n",
            f"{prefix}old-run/task/instruction.md": "stale instruction",
        },
        listed=[f"{prefix}old-run/task/instruction.md"],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_summary_inputs(_trial(prefix=prefix, attempts=4))
    )

    assert result == (
        {"trial_name": "current-attempt"},
        "repair the broker",
        "PASS\n",
    )
    assert storage.download_calls.count(f"{prefix}result.json") == 1
    assert storage.list_calls == 0


def test_manifest_missing_summary_artifacts_never_reads_a_sibling(monkeypatch):
    prefix = "tasks/task-1/trials/task-1-7/attempt-5/"
    stale_key = f"{prefix}old-run/task/instruction.md"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            stale_key: "stale instruction",
        },
        listed=[stale_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_summary_inputs(_trial(prefix=prefix, attempts=5))
    )

    assert result == (None, None, None)
    assert stale_key not in storage.download_calls
    assert storage.list_calls == 0


def test_agent_file_uses_manifest_directory_without_sibling_fallback(monkeypatch):
    prefix = "tasks/task-1/trials/task-1-7/attempt-6/"
    exact_key = f"{prefix}current-run/agent/screenshot.png"
    stale_key = f"{prefix}old-run/agent/screenshot.png"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            exact_key: "current image",
            stale_key: "stale image",
        },
        listed=[stale_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    content, media_type = asyncio.run(
        trial_io.read_trial_agent_file(
            _trial(prefix=prefix, attempts=6),
            "screenshot.png",
        )
    )

    assert content == b"current image"
    assert media_type == "image/png"
    assert stale_key not in storage.download_calls
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


def test_missing_attempt_pointer_never_selects_a_sibling_attempt(monkeypatch):
    prefix = "tasks/task-1/trials/task-1-7/"
    for namespace in ("attempt", "analysis-qa/attempt"):
        _clear_cache()
        old_key = f"{prefix}{namespace}-1/old-run/agent/trajectory.json"
        partial_current_key = (
            f"{prefix}{namespace}-2/current-run/agent/setup/stdout.txt"
        )
        storage = _Storage(
            {
                old_key: json.dumps({"trial_name": "old-attempt"}),
                partial_current_key: "setup completed",
            },
            listed=[old_key, partial_current_key],
        )
        monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

        result = asyncio.run(
            trial_io.read_trial_trajectory(_trial(prefix=None, attempts=2))
        )

        assert result is None
        assert old_key not in storage.download_calls
        assert storage.list_calls == 1


def test_missing_pointer_still_recovers_a_pre_attempt_historical_layout(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/"
    historical_key = f"{prefix}historical-run/agent/trajectory.json"
    storage = _Storage(
        {historical_key: json.dumps({"trial_name": "historical"})},
        listed=[historical_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(trial_io.read_trial_trajectory(_trial(prefix=None)))

    assert result == {"trial_name": "historical"}
    assert storage.download_calls.count(historical_key) == 1
    assert storage.list_calls == 1


def test_missing_pointer_rejects_a_legacy_root_manifest_beside_new_attempts(
    monkeypatch,
):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/"
    manifest_key = f"{prefix}result.json"
    stale_key = f"{prefix}legacy-run/agent/trajectory.json"
    attempt_key = f"{prefix}attempt-2/current-run/agent/setup/stdout.txt"
    storage = _Storage(
        {
            manifest_key: json.dumps({"trial_results": [{"trial_name": "legacy-run"}]}),
            stale_key: json.dumps({"trial_name": "legacy"}),
            attempt_key: "setup completed",
        },
        listed=[manifest_key, stale_key, attempt_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_trajectory(_trial(prefix=None, attempts=2))
    )

    assert result is None
    assert manifest_key not in storage.download_calls
    assert stale_key not in storage.download_calls
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

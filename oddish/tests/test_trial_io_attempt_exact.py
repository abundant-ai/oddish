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
        self.log_prefixes: list[str] = []

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

    async def list_keys(self, prefix: str) -> list[str]:
        self.list_calls += 1
        return [key for key in self.listed if key.startswith(prefix)]

    async def download_trial_logs(self, prefix: str) -> str:
        self.log_prefixes.append(prefix)
        return "\n".join(
            value for key, value in self.objects.items() if key.startswith(prefix)
        )


def _trial(*, prefix: str | None, attempts: int = 1):
    return SimpleNamespace(
        id="task-1-7",
        name="display-name-not-used-for-current-layout",
        model="openai/gpt-5.5",
        trial_s3_key=prefix,
        harbor_result_path=None,
        error_message=None,
        finished_at=datetime.now(timezone.utc),
        attempts=attempts,
    )


def _clear_cache() -> None:
    trial_io._TRAJECTORY_CACHE.clear()
    trial_io._TRAJECTORY_LOCKS.clear()
    trial_io._STRUCTURED_LOGS_CACHE.clear()
    trial_io._STRUCTURED_LOGS_LOCKS.clear()
    trial_io._PROBE_ARTIFACTS_CACHE.clear()
    trial_io._PROBE_ARTIFACTS_LOCKS.clear()


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


def test_exact_attempt_never_falls_back_to_a_stale_local_trajectory(
    monkeypatch, tmp_path
):
    _clear_cache()
    jobs_dir = tmp_path / "jobs"
    job_dir = jobs_dir / "job-1"
    local_trial_dir = job_dir / "local-run"
    (local_trial_dir / "agent").mkdir(parents=True)
    result_path = job_dir / "result.json"
    result_path.write_text(json.dumps({"trial_results": [{"trial_name": "local-run"}]}))
    (local_trial_dir / "agent" / "trajectory.json").write_text(
        json.dumps({"trial_name": "stale-local"})
    )
    monkeypatch.setattr(trial_io.settings, "harbor_jobs_dir", str(jobs_dir))

    prefix = "tasks/task-1/trials/task-1-7/attempt-3/"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            )
        }
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = _trial(prefix=prefix, attempts=3)
    trial.harbor_result_path = str(result_path)

    result = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert result is None


def test_trajectory_storage_outage_is_not_reported_as_a_missing_artifact(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-3/"
    trajectory_key = f"{prefix}current-run/agent/trajectory.json"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            trajectory_key: "unused",
        }
    )

    async def unavailable_download(key: str) -> str:
        if key == trajectory_key:
            raise TimeoutError("storage timed out")
        return storage.objects[key]

    storage.download_text = unavailable_download
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    try:
        asyncio.run(trial_io.read_trial_trajectory(_trial(prefix=prefix, attempts=3)))
    except TimeoutError as exc:
        assert str(exc) == "storage timed out"
    else:
        raise AssertionError("an exact-attempt storage outage must propagate")


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


def test_result_reader_returns_the_exact_attempt_manifest(monkeypatch):
    prefix = "tasks/task-1/trials/task-1-7/attempt-6/"
    manifest = {"trial_results": [{"trial_name": "current-run"}], "attempt": 6}
    storage = _Storage({f"{prefix}result.json": json.dumps(manifest)})
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(trial_io.read_trial_result(_trial(prefix=prefix, attempts=6)))

    assert result == manifest


def test_ambiguous_manifest_exposes_neither_result_nor_sibling_artifacts(monkeypatch):
    from fastapi import HTTPException

    prefix = "tasks/task-1/trials/task-1-7/attempt-6/"
    stale_key = f"{prefix}old-run/agent/screenshot.png"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {
                    "trial_results": [
                        {"trial_name": "run-1"},
                        {"trial_name": "run-2"},
                    ]
                }
            ),
            stale_key: "stale image",
        },
        listed=[stale_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = _trial(prefix=prefix, attempts=6)

    for operation in (
        lambda: trial_io.read_trial_result(trial),
        lambda: trial_io.read_trial_agent_file(trial, "screenshot.png"),
    ):
        try:
            asyncio.run(operation())
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("an ambiguous manifest must expose no artifacts")
    assert stale_key not in storage.download_calls


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
    from fastapi import HTTPException

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

    try:
        asyncio.run(trial_io.read_trial_result(_trial(prefix=None, attempts=2)))
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("a stale root result must not survive attempt namespaces")


def test_missing_pointer_blocks_every_local_fallback(monkeypatch, tmp_path):
    _clear_cache()
    jobs_dir = tmp_path / "jobs"
    job_dir = jobs_dir / "job-1"
    local_trial_dir = job_dir / "local-run"
    (local_trial_dir / "agent").mkdir(parents=True)
    (local_trial_dir / "verifier").mkdir()
    result_path = job_dir / "result.json"
    result_path.write_text(json.dumps({"trial_results": [{"trial_name": "local-run"}]}))
    (local_trial_dir / "agent" / "trajectory.json").write_text(
        json.dumps({"trial_name": "stale-local"})
    )
    (local_trial_dir / "verifier" / "test-stdout.txt").write_text("STALE LOCAL\n")
    monkeypatch.setattr(trial_io.settings, "harbor_jobs_dir", str(jobs_dir))

    root = "tasks/task-1/trials/task-1-7/"
    attempt_key = f"{root}attempt-2/current-run/agent/setup/stdout.txt"
    storage = _Storage({attempt_key: "setup"}, listed=[attempt_key])
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = _trial(prefix=None, attempts=2)
    trial.harbor_result_path = str(result_path)

    trajectory = asyncio.run(trial_io.read_trial_trajectory(trial))
    summary = asyncio.run(trial_io.read_trial_summary_inputs(trial))
    freeform = asyncio.run(trial_io.read_trial_logs(trial))
    structured = asyncio.run(trial_io.read_trial_logs_structured(trial))

    assert trajectory is None
    assert summary == (None, None, None)
    assert freeform["logs"] == ""
    assert structured["verifier"]["stdout"] is None


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


def test_probe_cache_changes_with_the_attempt_pointer(monkeypatch):
    _clear_cache()
    calls = []

    async def read_uncached(trial):
        calls.append(trial.trial_s3_key)
        return {"watchdog_log": trial.trial_s3_key}

    monkeypatch.setattr(trial_io, "_read_trial_probe_artifacts_uncached", read_uncached)
    trial = _trial(prefix="attempt-1/", attempts=1)

    first = asyncio.run(trial_io.read_trial_probe_artifacts(trial))
    trial.attempts = 2
    trial.trial_s3_key = "attempt-2/"
    second = asyncio.run(trial_io.read_trial_probe_artifacts(trial))

    assert first == {"watchdog_log": "attempt-1/"}
    assert second == {"watchdog_log": "attempt-2/"}
    assert calls == ["attempt-1/", "attempt-2/"]


def test_structured_logs_use_the_manifest_selected_harbor_directory(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    current_key = f"{prefix}current-run/verifier/test-stdout.txt"
    stale_key = f"{prefix}old-run/verifier/test-stdout.txt"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            current_key: "CURRENT\n",
            stale_key: "STALE\n",
        },
        listed=[stale_key, current_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_logs_structured(_trial(prefix=prefix, attempts=2))
    )

    assert result["verifier"]["stdout"] == "CURRENT\n"
    assert stale_key not in storage.download_calls


def test_freeform_logs_use_the_manifest_selected_harbor_directory(monkeypatch):
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    current_prefix = f"{prefix}current-run/"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            f"{current_prefix}verifier/test-stdout.txt": "CURRENT\n",
            f"{prefix}old-run/verifier/test-stdout.txt": "STALE\n",
        }
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(trial_io.read_trial_logs(_trial(prefix=prefix, attempts=2)))

    assert result["s3_key"] == current_prefix
    assert result["logs"] == "CURRENT\n"
    assert storage.log_prefixes == [current_prefix]


def test_root_only_failure_manifest_reads_exact_attempt_logs(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    log_key = f"{prefix}modal-output.log"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps({"trial_results": []}),
            log_key: "image build failed\n",
        },
        listed=[log_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    freeform = asyncio.run(trial_io.read_trial_logs(_trial(prefix=prefix, attempts=2)))
    structured = asyncio.run(
        trial_io.read_trial_logs_structured(_trial(prefix=prefix, attempts=2))
    )

    assert freeform["s3_key"] == prefix
    assert "image build failed" in freeform["logs"]
    assert storage.log_prefixes == [prefix]
    assert structured["other"] == [
        {"name": "modal-output.log", "content": "image build failed\n"}
    ]


def test_malformed_manifest_does_not_expose_sibling_logs(monkeypatch):
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    stale_key = f"{prefix}old-run/verifier/test-stdout.txt"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps({"trial_results": [{}]}),
            stale_key: "STALE\n",
        },
        listed=[stale_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(trial_io.read_trial_logs(_trial(prefix=prefix, attempts=2)))

    assert result["logs"] == ""
    assert storage.log_prefixes == []
    assert stale_key not in storage.download_calls


def test_structured_logs_with_a_missing_pointer_do_not_scan_sibling_attempts(
    monkeypatch,
):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/"
    stale_key = f"{prefix}attempt-1/old-run/verifier/test-stdout.txt"
    current_key = f"{prefix}attempt-2/current-run/agent/setup/stdout.txt"
    storage = _Storage(
        {stale_key: "STALE\n", current_key: "setup complete\n"},
        listed=[stale_key, current_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_logs_structured(_trial(prefix=None, attempts=2))
    )

    assert result["verifier"]["stdout"] is None
    assert stale_key not in storage.download_calls
    assert current_key not in storage.download_calls


def test_freeform_logs_with_a_missing_pointer_do_not_scan_sibling_attempts(
    monkeypatch,
):
    prefix = "tasks/task-1/trials/task-1-7/"
    stale_key = f"{prefix}attempt-1/old-run/verifier/test-stdout.txt"
    current_key = f"{prefix}attempt-2/current-run/agent/setup/stdout.txt"
    storage = _Storage(
        {stale_key: "STALE\n", current_key: "setup complete\n"},
        listed=[stale_key, current_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(trial_io.read_trial_logs(_trial(prefix=None, attempts=2)))

    assert result["logs"] == ""
    assert storage.log_prefixes == []


def test_structured_log_cache_changes_with_the_attempt_pointer(monkeypatch):
    _clear_cache()
    first_prefix = "tasks/task-1/trials/task-1-7/attempt-1/"
    second_prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    first_key = f"{first_prefix}run-1/verifier/test-stdout.txt"
    second_key = f"{second_prefix}run-2/verifier/test-stdout.txt"
    storage = _Storage(
        {
            f"{first_prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "run-1"}]}
            ),
            f"{second_prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "run-2"}]}
            ),
            first_key: "FIRST\n",
            second_key: "SECOND\n",
        },
        listed=[first_key, second_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = _trial(prefix=first_prefix, attempts=1)

    first = asyncio.run(trial_io.read_trial_logs_structured(trial))
    trial.attempts = 2
    trial.trial_s3_key = second_prefix
    second = asyncio.run(trial_io.read_trial_logs_structured(trial))

    assert first["verifier"]["stdout"] == "FIRST\n"
    assert second["verifier"]["stdout"] == "SECOND\n"

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from oddish.core import trial_io
from oddish.core.harbor_artifacts import (
    ODDISH_TRIAL_NAME_KEY,
    write_trial_selection_manifest,
)
from oddish.core.trial_artifacts import (
    AnalysisArtifactLayoutError,
    TrialArtifactMode,
    resolve_trial_artifact_layout,
    validate_uploaded_analysis_artifacts,
)


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


class _ClientErrorStorage(_Storage):
    async def download_text(self, key: str) -> str:
        self.download_calls.append(key)
        try:
            return self.objects[key]
        except KeyError as exc:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                "GetObject",
            ) from exc


class _BinaryStorage(_Storage):
    def __init__(self, objects: dict[str, str | bytes], listed: list[str]):
        super().__init__({}, listed=listed)
        self.binary_objects = objects

    async def download_text(self, key: str) -> str:
        self.download_calls.append(key)
        value = self.binary_objects[key]
        if isinstance(value, str):
            return value
        return value.decode("utf-8")

    async def download_bytes(self, key: str) -> bytes:
        value = self.binary_objects[key]
        return value.encode() if isinstance(value, str) else value

    async def object_exists(self, key: str) -> bool:
        return key in self.binary_objects


def _trial(*, prefix: str | None, attempts: int = 1, kind: str = "agent"):
    return SimpleNamespace(
        id="task-1-7",
        name="display-name-not-used-for-current-layout",
        model="openai/gpt-5.5",
        trial_s3_key=prefix,
        harbor_result_path=None,
        error_message=None,
        finished_at=datetime.now(UTC),
        attempts=attempts,
        kind=kind,
    )


def _clear_cache() -> None:
    trial_io._TRAJECTORY_CACHE.clear()
    trial_io._TRAJECTORY_LOCKS.clear()
    trial_io._STRUCTURED_LOGS_CACHE.clear()
    trial_io._STRUCTURED_LOGS_LOCKS.clear()
    trial_io._PROBE_ARTIFACTS_CACHE.clear()
    trial_io._PROBE_ARTIFACTS_LOCKS.clear()


def test_analysis_upload_validation_accepts_manifest_selected_qa_artifacts():
    prefix = "tasks/task-1/trials/task-1-qa/analysis-qa/attempt-2/"
    child = f"{prefix}qa-run/"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "qa-run"}]}
            ),
            f"{child}verifier/qa_result.json": "{}",
            f"{child}agent/trajectory.json": "{}",
        }
    )

    layout = asyncio.run(
        validate_uploaded_analysis_artifacts(
            trial_id="task-1-qa",
            trial_s3_key=prefix,
            required_artifact="qa_result.json",
            has_trajectory=True,
            storage=storage,
        )
    )

    assert layout.mode is TrialArtifactMode.EXACT
    assert layout.artifact_prefix == child


def test_current_harbor_root_manifest_records_and_selects_its_only_child(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "id": "67b598d1-9dc2-434e-861a-f18baea0a8dd",
                "n_total_trials": 1,
                "stats": {"n_completed_trials": 1},
            }
        )
    )

    assert write_trial_selection_manifest(result_path, ["qa-run__AbC1234"])
    manifest = json.loads(result_path.read_text())

    assert manifest[ODDISH_TRIAL_NAME_KEY] == "qa-run__AbC1234"
    assert "trial_results" not in manifest

    prefix = "tasks/task-1/trials/task-1-qa/analysis-qa/attempt-2/"
    child = f"{prefix}qa-run__AbC1234/"
    storage = _Storage(
        {
            f"{prefix}result.json": result_path.read_text(),
            f"{child}verifier/qa_result.json": "{}",
            f"{child}agent/trajectory.json": "{}",
        }
    )
    layout = asyncio.run(
        validate_uploaded_analysis_artifacts(
            trial_id="task-1-qa",
            trial_s3_key=prefix,
            required_artifact="qa_result.json",
            has_trajectory=True,
            storage=storage,
        )
    )

    assert layout.mode is TrialArtifactMode.EXACT
    assert layout.artifact_prefix == child


def test_current_harbor_root_manifest_does_not_guess_between_children(tmp_path):
    result_path = tmp_path / "result.json"
    original = {"n_total_trials": 2, "stats": {"n_completed_trials": 2}}
    result_path.write_text(json.dumps(original))

    assert not write_trial_selection_manifest(result_path, ["qa-a", "qa-b"])
    assert json.loads(result_path.read_text()) == original


@pytest.mark.parametrize(
    "prefix",
    [
        "tasks/task-1/trials/task-1-7/",
        "tasks/task-1/trials/task-1-7/analysis-qa/",
    ],
)
def test_selectorless_harbor_root_uses_legacy_mode_for_historical_prefix(prefix):
    manifest = {"id": "job-1", "n_total_trials": 1, "stats": {}}
    storage = _Storage({f"{prefix}result.json": json.dumps(manifest)})

    layout = asyncio.run(resolve_trial_artifact_layout(_trial(prefix=prefix), storage))

    assert layout.mode is TrialArtifactMode.LEGACY
    assert layout.attempt_prefix == prefix
    assert layout.artifact_prefix == prefix
    assert layout.manifest == manifest
    assert layout.failure_reason == "legacy Harbor root manifest has no trial selector"


def test_selectorless_historical_manifest_keeps_legacy_trajectory_readable(
    monkeypatch,
):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/"
    trajectory_key = f"{prefix}harbor-run/agent/trajectory.json"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"id": "job-1", "n_total_trials": 1, "stats": {}}
            ),
            trajectory_key: json.dumps({"trial_name": "historical-attempt"}),
        },
        listed=[trajectory_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(trial_io.read_trial_trajectory(_trial(prefix=prefix)))

    assert result == {"trial_name": "historical-attempt"}
    assert storage.list_calls == 1


def test_selectorless_harbor_root_is_unavailable_for_attempt_scoped_prefix():
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"id": "job-1", "n_total_trials": 1, "stats": {}}
            )
        }
    )

    layout = asyncio.run(
        resolve_trial_artifact_layout(_trial(prefix=prefix, attempts=2), storage)
    )

    assert layout.mode is TrialArtifactMode.UNAVAILABLE
    assert layout.artifact_prefix is None
    assert layout.failure_reason == "result.json trial_results must be a list"


@pytest.mark.parametrize(
    ("manifest", "expected_error"),
    [
        ("not-json", "result.json is invalid JSON"),
        (
            json.dumps({"id": "job-1", "n_total_trials": 1, "stats": {}}),
            "result.json trial_results must be a list",
        ),
        (
            json.dumps({"trial_results": []}),
            "result.json contains no trial_results",
        ),
        (
            json.dumps({"trial_results": [{}, {}]}),
            "result.json must identify exactly one Harbor trial",
        ),
    ],
)
def test_analysis_upload_validation_reports_invalid_manifest_reason(
    manifest, expected_error
):
    prefix = "tasks/task-1/trials/task-1-qa/analysis-qa/attempt-1/"
    storage = _Storage(
        {
            f"{prefix}result.json": manifest,
            f"{prefix}verifier/qa_result.json": "{}",
        },
        listed=[f"{prefix}result.json", f"{prefix}verifier/qa_result.json"],
    )

    with pytest.raises(AnalysisArtifactLayoutError) as exc_info:
        asyncio.run(
            validate_uploaded_analysis_artifacts(
                trial_id="task-1-qa",
                trial_s3_key=prefix,
                required_artifact="qa_result.json",
                has_trajectory=False,
                storage=storage,
            )
        )

    message = str(exc_info.value)
    assert expected_error in message
    assert f"prefix={prefix!r}" in message
    assert "uploaded_files=['result.json', 'verifier/qa_result.json']" in message


def test_analysis_upload_validation_reports_missing_manifest():
    prefix = "tasks/task-1/trials/task-1-qa/analysis-qa/attempt-1/"
    child_key = f"{prefix}qa-run/verifier/qa_result.json"
    storage = _Storage({child_key: "{}"}, listed=[child_key])

    with pytest.raises(AnalysisArtifactLayoutError) as exc_info:
        asyncio.run(
            validate_uploaded_analysis_artifacts(
                trial_id="task-1-qa",
                trial_s3_key=prefix,
                required_artifact="qa_result.json",
                has_trajectory=False,
                storage=storage,
            )
        )

    message = str(exc_info.value)
    assert "result.json is missing" in message
    assert "uploaded_files=['qa-run/verifier/qa_result.json']" in message


def test_analysis_upload_validation_never_accepts_a_sibling_child():
    prefix = "tasks/task-1/trials/task-1-qa/analysis-qa/attempt-2/"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            f"{prefix}old-run/verifier/qa_result.json": "{}",
        }
    )

    with pytest.raises(
        AnalysisArtifactLayoutError, match="missing verifier/qa_result.json"
    ):
        asyncio.run(
            validate_uploaded_analysis_artifacts(
                trial_id="task-1-qa",
                trial_s3_key=prefix,
                required_artifact="qa_result.json",
                has_trajectory=False,
                storage=storage,
            )
        )


def test_analysis_upload_validation_requires_reported_trajectory():
    prefix = "tasks/task-1/trials/task-1-audit/analysis-audit/attempt-3/"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "audit-run"}]}
            ),
            f"{prefix}audit-run/verifier/audit_result.json": "{}",
        }
    )

    with pytest.raises(AnalysisArtifactLayoutError, match="trajectory"):
        asyncio.run(
            validate_uploaded_analysis_artifacts(
                trial_id="task-1-audit",
                trial_s3_key=prefix,
                required_artifact="audit_result.json",
                has_trajectory=True,
                storage=storage,
            )
        )


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


def test_exact_attempt_recovers_malformed_claude_trajectory_from_stream(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-3/"
    child = f"{prefix}current-run/"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            f"{child}agent/trajectory.json": "{malformed",
            f"{child}agent/claude-code.txt": "captured stream",
        }
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    converted: list[tuple[str, str | None]] = []

    def convert(text: str, *, model_name: str | None):
        converted.append((text, model_name))
        return {"session_id": "recovered", "steps": []}

    monkeypatch.setattr(
        trial_io,
        "convert_claude_code_stream_text_to_trajectory",
        convert,
    )

    result = asyncio.run(
        trial_io.read_trial_trajectory(_trial(prefix=prefix, attempts=3))
    )

    assert result == {"session_id": "recovered", "steps": []}
    assert converted == [("captured stream", "openai/gpt-5.5")]
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
    for namespace, kind in (("attempt", "agent"), ("analysis-qa/attempt", "qa")):
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
        monkeypatch.setattr(
            trial_io,
            "get_storage_client",
            lambda storage=storage: storage,
        )

        result = asyncio.run(
            trial_io.read_trial_trajectory(_trial(prefix=None, attempts=2, kind=kind))
        )

        assert result is None
        assert old_key not in storage.download_calls
        assert storage.list_calls == 1


@pytest.mark.parametrize(
    ("kind", "namespaces", "selected_namespace"),
    (
        ("agent", ("attempt-1", "analysis-qa/attempt-1"), "attempt-1"),
        ("qa", ("attempt-1", "analysis-qa/attempt-1"), "analysis-qa/attempt-1"),
        ("agent", ("analysis-qa/attempt-1",), None),
        ("qa", ("attempt-1",), None),
    ),
)
def test_missing_pointer_selects_only_the_trial_kinds_attempt_namespace(
    monkeypatch,
    kind,
    namespaces,
    selected_namespace,
):
    _clear_cache()
    root_prefix = "tasks/task-1/trials/task-1-7/"
    objects = {}
    run_names = {}
    for index, namespace in enumerate(namespaces):
        run_name = f"run-{index}"
        run_names[namespace] = run_name
        objects[f"{root_prefix}{namespace}/result.json"] = json.dumps(
            {"trial_results": [{"trial_name": run_name}]}
        )
    storage = _Storage(objects, listed=list(objects))
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    layout = asyncio.run(
        resolve_trial_artifact_layout(
            _trial(prefix=None, kind=kind),
            storage,
        )
    )

    if selected_namespace is None:
        assert layout.mode is TrialArtifactMode.UNAVAILABLE
        assert storage.download_calls == []
    else:
        selected_prefix = f"{root_prefix}{selected_namespace}/"
        assert layout.mode is TrialArtifactMode.EXACT
        assert layout.attempt_prefix == selected_prefix
        assert layout.artifact_prefix == (
            f"{selected_prefix}{run_names[selected_namespace]}/"
        )
        assert storage.download_calls == [f"{selected_prefix}result.json"]


def test_missing_pointer_recovers_one_settled_matching_attempt(monkeypatch):
    _clear_cache()
    root_prefix = "tasks/task-1/trials/task-1-7/"
    attempt_prefix = f"{root_prefix}attempt-1/"
    child = f"{attempt_prefix}current-run/"
    trajectory_key = f"{child}agent/trajectory.json"
    objects = {
        f"{attempt_prefix}result.json": json.dumps(
            {"trial_results": [{"trial_name": "current-run"}]}
        ),
        trajectory_key: json.dumps({"trial_name": "recovered-attempt"}),
    }
    storage = _Storage(objects, listed=list(objects))
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_trajectory(_trial(prefix=None, attempts=1))
    )

    assert result == {"trial_name": "recovered-attempt"}
    assert storage.list_calls == 1
    assert trajectory_key in storage.download_calls


def test_missing_pointer_does_not_recover_one_stale_attempt(monkeypatch):
    _clear_cache()
    root_prefix = "tasks/task-1/trials/task-1-7/"
    attempt_prefix = f"{root_prefix}attempt-1/"
    child = f"{attempt_prefix}old-run/"
    trajectory_key = f"{child}agent/trajectory.json"
    objects = {
        f"{attempt_prefix}result.json": json.dumps(
            {"trial_results": [{"trial_name": "old-run"}]}
        ),
        trajectory_key: json.dumps({"trial_name": "stale-attempt"}),
    }
    storage = _Storage(objects, listed=list(objects))
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_trajectory(_trial(prefix=None, attempts=2))
    )

    assert result is None
    assert trajectory_key not in storage.download_calls


def test_missing_pointer_does_not_recover_an_unsettled_attempt(monkeypatch):
    _clear_cache()
    root_prefix = "tasks/task-1/trials/task-1-7/"
    attempt_prefix = f"{root_prefix}attempt-1/"
    child = f"{attempt_prefix}current-run/"
    trajectory_key = f"{child}agent/trajectory.json"
    objects = {
        f"{attempt_prefix}result.json": json.dumps(
            {"trial_results": [{"trial_name": "current-run"}]}
        ),
        trajectory_key: json.dumps({"trial_name": "still-uploading"}),
    }
    storage = _Storage(objects, listed=list(objects))
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = _trial(prefix=None, attempts=1)
    trial.finished_at = None

    result = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert result is None
    assert trajectory_key not in storage.download_calls


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


def test_legacy_layout_recovers_malformed_claude_trajectory_from_stream(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/"
    child = f"{prefix}display-name-not-used-for-current-layout/"
    storage = _Storage(
        {
            f"{child}agent/trajectory.json": "{malformed",
            f"{child}agent/claude-code.txt": "captured historical stream",
        }
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    monkeypatch.setattr(
        trial_io,
        "convert_claude_code_stream_text_to_trajectory",
        lambda text, *, model_name: {
            "session_id": "recovered-historical",
            "steps": [],
        },
    )
    trial = _trial(prefix=prefix)
    trial.has_trajectory = True

    result = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert result == {"session_id": "recovered-historical", "steps": []}


def test_legacy_readers_continue_after_missing_s3_candidates(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/"
    trial_prefix = f"{prefix}display-name-not-used-for-current-layout/"
    trajectory_key = f"{trial_prefix}agent/trajectory.json"
    instruction_key = f"{trial_prefix}task/instruction.md"
    verifier_key = f"{trial_prefix}verifier/test-stdout.txt"
    screenshot_key = f"{trial_prefix}agent/screenshot.png"
    storage = _ClientErrorStorage(
        {
            trajectory_key: json.dumps({"trial_name": "historical"}),
            instruction_key: "repair the broker",
            verifier_key: "PASS\n",
            screenshot_key: "image bytes",
        },
        listed=[screenshot_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = _trial(prefix=prefix)

    summary_inputs = asyncio.run(trial_io.read_trial_summary_inputs(trial))
    screenshot, media_type = asyncio.run(
        trial_io.read_trial_agent_file(trial, "screenshot.png")
    )

    assert summary_inputs == (
        {"trial_name": "historical"},
        "repair the broker",
        "PASS\n",
    )
    assert screenshot == b"image bytes"
    assert media_type == "image/png"
    assert f"{prefix}agent/trajectory.json" in storage.download_calls
    assert trajectory_key in storage.download_calls
    assert f"{prefix}agent/screenshot.png" in storage.download_calls
    assert screenshot_key in storage.download_calls


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


def test_verifier_only_logs_ignore_broken_auxiliary_session_files(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    child = f"{prefix}current-run/"
    stdout_key = f"{child}verifier/test-stdout.txt"
    broken_session_key = f"{child}agent/session/history.log"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            stdout_key: "PASS\n",
        },
        listed=[stdout_key, broken_session_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_logs_structured(
            _trial(prefix=prefix, attempts=2), verifier_only=True
        )
    )

    assert result["verifier"] == {"stdout": "PASS\n", "stderr": None}
    assert broken_session_key not in storage.download_calls


def test_verifier_only_logs_read_legacy_stdout_filename(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    child = f"{prefix}current-run/"
    stdout_key = f"{child}verifier/stdout.txt"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            stdout_key: "legacy verifier output\n",
        },
        listed=[stdout_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_logs_structured(
            _trial(prefix=prefix, attempts=2), verifier_only=True
        )
    )

    assert result["verifier"] == {
        "stdout": "legacy verifier output\n",
        "stderr": None,
    }


def test_verifier_only_logs_keep_pre_sandbox_database_exception(monkeypatch):
    _clear_cache()
    storage = _Storage({})
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = _trial(prefix=None)
    trial.error_message = "Failed to create sandbox: CPU capacity exhausted"

    result = asyncio.run(trial_io.read_trial_logs_structured(trial, verifier_only=True))

    assert result["verifier"] == {"stdout": None, "stderr": None}
    assert result["exception"] == "Failed to create sandbox: CPU capacity exhausted"


def test_verifier_only_logs_leave_missing_verifier_evidence_null(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    child = f"{prefix}current-run/"
    session_key = f"{child}agent/session/history.log"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            session_key: "agent history\n",
        },
        listed=[session_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_logs_structured(
            _trial(prefix=prefix, attempts=2), verifier_only=True
        )
    )

    assert result["verifier"] == {"stdout": None, "stderr": None}
    assert result["exception"] is None


def test_structured_log_cache_separates_verifier_only_and_full_responses(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    child = f"{prefix}current-run/"
    stdout_key = f"{child}verifier/test-stdout.txt"
    session_key = f"{child}agent/session/history.log"
    storage = _Storage(
        {
            f"{prefix}result.json": json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            stdout_key: "PASS\n",
            session_key: "agent history\n",
        },
        listed=[stdout_key, session_key],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial = _trial(prefix=prefix, attempts=2)

    verifier_only = asyncio.run(
        trial_io.read_trial_logs_structured(trial, verifier_only=True)
    )
    full = asyncio.run(trial_io.read_trial_logs_structured(trial))

    assert verifier_only["other"] == []
    assert full["other"] == [
        {"name": "agent/session/history.log", "content": "agent history\n"}
    ]


def test_structured_logs_skip_binary_artifacts_and_keep_verifier_evidence(monkeypatch):
    _clear_cache()
    prefix = "tasks/task-1/trials/task-1-7/attempt-2/"
    child = f"{prefix}current-run/"
    manifest_key = f"{prefix}result.json"
    stdout_key = f"{child}verifier/test-stdout.txt"
    auxiliary_log_key = f"{child}agent/custom.log"
    sqlite_key = f"{child}agent/grok-session/sessions/session_search.sqlite"
    compressed_key = f"{child}verifier/produced/compress/always_form.out"
    storage = _BinaryStorage(
        {
            manifest_key: json.dumps(
                {"trial_results": [{"trial_name": "current-run"}]}
            ),
            stdout_key: b"grader output: failed byte \xff\n",
            auxiliary_log_key: b"agent output: failed byte \xfe\n",
            sqlite_key: b"SQLite format 3\x00\x8a",
            compressed_key: b"POST / HTTP/1.1\n\x9c\x01",
        },
        listed=[
            manifest_key,
            stdout_key,
            auxiliary_log_key,
            sqlite_key,
            compressed_key,
        ],
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)

    result = asyncio.run(
        trial_io.read_trial_logs_structured(_trial(prefix=prefix, attempts=2))
    )

    assert result["verifier"]["stdout"] == "grader output: failed byte �\n"
    assert result["other"] == [
        {
            "name": "agent/custom.log",
            "content": "agent output: failed byte �\n",
        }
    ]
    assert sqlite_key in storage.download_calls
    assert compressed_key in storage.download_calls


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

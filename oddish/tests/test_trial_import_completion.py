from contextlib import asynccontextmanager
import io
import tarfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import oddish.queue as queue_module
from oddish.core.ingest import trial_imports
from oddish.core.trial_artifacts import TrialArtifactLayout, TrialArtifactMode
from oddish.db import TrialModel, TrialOrigin
from oddish.db.storage import StorageClient


class _Session:
    def __init__(self, trial, events):
        self.trial = trial
        self.events = events

    async def get(self, model, row_id):
        assert model is TrialModel
        assert row_id == self.trial.id
        return self.trial

    async def commit(self):
        self.events.append("commit")


class _Storage:
    def __init__(self, events, *, archive_exists=True, extracted_keys=()):
        self.events = events
        self.archive_exists = archive_exists
        self.extracted_keys = list(extracted_keys)

    async def object_exists(self, key):
        assert key.endswith(".oddish-trial-import.tar.gz")
        self.events.append("check_archive")
        return self.archive_exists

    async def extract_trial_import_archive(self, trial_id):
        self.events.append("extract")
        return 3

    async def list_keys(self, prefix):
        self.events.append("list_extracted")
        return [key for key in self.extracted_keys if key.startswith(prefix)]

    async def delete_trial_import_archive(self, trial_id):
        self.events.append("delete_archive")


class _S3:
    def __init__(self):
        self.put_keys = []
        self.deleted_keys = []

    async def put_object(self, *, Bucket, Key, Body):
        self.put_keys.append(Key)

    async def delete_object(self, *, Bucket, Key):
        self.deleted_keys.append(Key)


def _session_context(session):
    @asynccontextmanager
    async def get_session():
        yield session

    return get_session


def _install_completion_fakes(
    monkeypatch,
    *,
    archive_exists=True,
    extracted_keys=(),
    layout_mode=TrialArtifactMode.EXACT,
    finalize_error=None,
):
    events = []
    trial_id = "task-1-7"
    prefix = StorageClient._trial_prefix(trial_id)
    trial = SimpleNamespace(
        id=trial_id,
        task_id="task-1",
        org_id="org-1",
        origin=TrialOrigin.IMPORTED,
        trial_s3_key=prefix,
    )
    session = _Session(trial, events)
    storage = _Storage(
        events,
        archive_exists=archive_exists,
        extracted_keys=extracted_keys,
    )

    async def resolve_layout(_trial, _storage):
        events.append("validate")
        return TrialArtifactLayout(
            mode=layout_mode,
            attempt_prefix=prefix,
            artifact_prefix=(
                None if layout_mode is TrialArtifactMode.UNAVAILABLE else prefix
            ),
            failure_reason=(
                "result.json is invalid JSON"
                if layout_mode is TrialArtifactMode.UNAVAILABLE
                else None
            ),
        )

    async def maybe_start_qa_stage(_session, settled_trial_id):
        assert settled_trial_id == trial_id
        events.append("finalize")
        if finalize_error is not None:
            raise finalize_error

    async def run_once(operation, *, what):
        assert what == "trial_import_complete"
        return await operation()

    monkeypatch.setattr(trial_imports, "get_session", _session_context(session))
    monkeypatch.setattr(trial_imports, "get_storage_client", lambda: storage)
    monkeypatch.setattr(trial_imports, "resolve_trial_artifact_layout", resolve_layout)
    monkeypatch.setattr(trial_imports, "run_with_deadlock_retry", run_once)
    monkeypatch.setattr(queue_module, "maybe_start_qa_stage", maybe_start_qa_stage)
    return trial_id, prefix, events


@pytest.mark.asyncio
async def test_extract_trial_import_archive_retains_staging(monkeypatch):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        content = b"{}"
        member = tarfile.TarInfo("result.json")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))

    s3 = _S3()
    storage = StorageClient()
    storage._client = s3

    async def object_exists(_key):
        return True

    async def download_bytes(_key):
        return archive.getvalue()

    monkeypatch.setattr(storage, "object_exists", object_exists)
    monkeypatch.setattr(storage, "download_bytes", download_bytes)

    extracted = await storage.extract_trial_import_archive("task-1-7")

    assert extracted == 1
    assert s3.put_keys == [f"{StorageClient._trial_prefix('task-1-7')}result.json"]
    assert s3.deleted_keys == []


@pytest.mark.asyncio
async def test_complete_trial_import_deletes_staging_after_finalization(monkeypatch):
    trial_id, prefix, events = _install_completion_fakes(monkeypatch)

    result = await trial_imports.complete_trial_import(
        trial_id=trial_id,
        org_id="org-1",
    )

    assert result.trial_s3_key == prefix
    assert result.files_extracted == 3
    assert events == [
        "check_archive",
        "extract",
        "validate",
        "finalize",
        "commit",
        "delete_archive",
    ]


@pytest.mark.asyncio
async def test_complete_trial_import_keeps_staging_when_layout_is_unreadable(
    monkeypatch,
):
    trial_id, _prefix, events = _install_completion_fakes(
        monkeypatch,
        layout_mode=TrialArtifactMode.UNAVAILABLE,
    )

    with pytest.raises(HTTPException) as exc:
        await trial_imports.complete_trial_import(trial_id=trial_id, org_id="org-1")

    assert exc.value.status_code == 400
    assert exc.value.detail == (
        "Imported trial artifacts are unreadable: result.json is invalid JSON"
    )
    assert events == ["check_archive", "extract", "validate"]


@pytest.mark.asyncio
async def test_complete_trial_import_keeps_staging_when_finalization_fails(
    monkeypatch,
):
    trial_id, _prefix, events = _install_completion_fakes(
        monkeypatch,
        finalize_error=RuntimeError("database unavailable"),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await trial_imports.complete_trial_import(trial_id=trial_id, org_id="org-1")

    assert events == ["check_archive", "extract", "validate", "finalize"]


@pytest.mark.asyncio
async def test_complete_trial_import_replays_after_staging_cleanup(monkeypatch):
    trial_id = "task-1-7"
    prefix = StorageClient._trial_prefix(trial_id)
    trial_id, _prefix, events = _install_completion_fakes(
        monkeypatch,
        archive_exists=False,
        extracted_keys=[f"{prefix}result.json"],
    )

    result = await trial_imports.complete_trial_import(
        trial_id=trial_id,
        org_id="org-1",
    )

    assert result.files_extracted == 0
    assert events == [
        "check_archive",
        "list_extracted",
        "validate",
        "finalize",
        "commit",
        "delete_archive",
    ]


@pytest.mark.asyncio
async def test_complete_trial_import_rejects_missing_archive_and_prefix(monkeypatch):
    trial_id, _prefix, events = _install_completion_fakes(
        monkeypatch,
        archive_exists=False,
    )

    with pytest.raises(HTTPException) as exc:
        await trial_imports.complete_trial_import(trial_id=trial_id, org_id="org-1")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Uploaded trial archive not found in S3"
    assert events == ["check_archive", "list_extracted"]

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from oddish.core import tasks
import oddish.queue as queue_mod
from oddish.db import TaskModel, TaskVersionModel
from oddish.schemas import TaskUploadCompleteRequest, TaskUploadInitRequest


class _Session:
    def __init__(self, *, task, current, trial_count=0):
        self.task = task
        self.current = current
        self.scalar_calls = 0
        self.trial_count = trial_count
        self.scalar_queries = []

    async def scalar(self, _query):
        self.scalar_calls += 1
        self.scalar_queries.append(_query)
        if "count(" in str(_query).lower():
            return self.trial_count
        if self.scalar_calls > 1:
            raise AssertionError("overwrite mode must not select the latest version")
        return self.task

    async def get(self, model, row_id, **_kwargs):
        if model is TaskModel:
            return self.task
        if model is TaskVersionModel and row_id == self.current.id:
            return self.current
        return None

    def add(self, _row):
        raise AssertionError("an in-place upload must not add a version row")

    async def commit(self):
        pass


class _Storage:
    def __init__(self, *, exists=True):
        self.exists = exists
        self.deleted: list[str] = []
        self.copied: list[tuple[str, str]] = []

    async def get_presigned_upload_url(self, key, **_kwargs):
        return f"https://storage.example/{key}"

    async def object_exists(self, _key):
        return self.exists

    async def copy_object(self, source, destination):
        self.copied.append((source, destination))

    async def delete_prefix(self, prefix):
        self.deleted.append(prefix)


def _session_context(session):
    @asynccontextmanager
    async def _get_session():
        yield session

    return _get_session


@pytest.fixture(autouse=True)
def _stub_source_change_invalidation(monkeypatch):
    calls = []

    async def fake_invalidate(_session, task):
        calls.append(task.id)

    monkeypatch.setattr(
        queue_mod, "invalidate_task_qa_for_source_change", fake_invalidate
    )
    return calls


def test_upload_version_modes_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        TaskUploadInitRequest(
            name="task",
            content_hash="new-hash",
            force_new_version=True,
            overwrite_current_version=True,
        )

    with pytest.raises(ValidationError, match="staging_key is required"):
        TaskUploadCompleteRequest(
            task_id="task-1",
            name="task",
            version=1,
            content_hash="new-hash",
            overwrite_current_version=True,
        )

    with pytest.raises(
        ValidationError, match="overwrite_base_content_hash is required"
    ):
        TaskUploadCompleteRequest(
            task_id="task-1",
            name="task",
            version=1,
            content_hash="new-hash",
            overwrite_current_version=True,
            staging_key=f"task-upload-staging/task-1/{'a' * 32}.tar.gz",
        )


@pytest.mark.asyncio
async def test_initialize_overwrite_targets_selected_version(monkeypatch) -> None:
    current = SimpleNamespace(
        id="task-1-v1",
        version=1,
        task_s3_key="tasks/task-1/v1/",
        content_hash="old-hash",
    )
    task = SimpleNamespace(id="task-1", current_version_id=current.id)
    session = _Session(task=task, current=current)
    storage = _Storage()
    monkeypatch.setattr(tasks, "get_session", _session_context(session))
    monkeypatch.setattr(tasks, "get_storage_client", lambda: storage)

    result = await tasks.initialize_task_upload(
        "task",
        content_hash="new-hash",
        overwrite_current_version=True,
    )

    assert result.task_id == "task-1"
    assert result.version == 1
    assert result.version_id == "task-1-v1"
    assert result.s3_key == "tasks/task-1/v1/"
    assert result.staging_key.startswith("task-upload-staging/task-1/")
    assert result.upload_url.endswith(result.staging_key)
    assert result.overwrite_base_content_hash == "old-hash"


@pytest.mark.asyncio
async def test_complete_overwrite_updates_row_and_invalidates_derived_state(
    monkeypatch,
) -> None:
    version = SimpleNamespace(
        id="task-1-v2",
        task_s3_key="tasks/task-1/v2/",
        content_hash="old-hash",
        message="old message",
        expanded_at=object(),
        expanded_manifest_key="tasks/task-1/v2-files/.oddish-manifest.json",
        pre_trial={"items": []},
        pre_trial_status=object(),
        pre_trial_error="old error",
        pre_trial_started_at=object(),
        pre_trial_finished_at=object(),
    )
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        current_version_id=version.id,
    )
    session = _Session(task=task, current=version)
    storage = _Storage()
    monkeypatch.setattr(tasks, "get_session", _session_context(session))
    monkeypatch.setattr(tasks, "get_storage_client", lambda: storage)
    monkeypatch.setattr(tasks.settings, "tasks_expand_archive", False)

    result = await tasks.complete_task_upload(
        task_id="task-1",
        task_name="task",
        version=2,
        content_hash="new-hash",
        org_id="org-1",
        overwrite_current_version=True,
        staging_key=f"task-upload-staging/task-1/{'a' * 32}.tar.gz",
        overwrite_base_content_hash="old-hash",
    )

    assert result.version == 2
    assert storage.copied == [
        (
            f"task-upload-staging/task-1/{'a' * 32}.tar.gz",
            f"tasks/task-1/v2-revisions/{'a' * 32}/.oddish-task.tar.gz",
        )
    ]
    assert storage.deleted == [f"task-upload-staging/task-1/{'a' * 32}.tar.gz"]
    assert version.content_hash == "new-hash"
    assert version.task_s3_key == f"tasks/task-1/v2-revisions/{'a' * 32}/"
    assert version.message == "old message"
    assert version.expanded_at is None
    assert version.expanded_manifest_key is None
    assert version.pre_trial is None
    assert version.pre_trial_status is None
    assert version.pre_trial_error is None
    assert version.pre_trial_started_at is None
    assert version.pre_trial_finished_at is None


@pytest.mark.asyncio
async def test_complete_overwrite_checks_current_before_promoting(monkeypatch) -> None:
    version = SimpleNamespace(id="task-1-v2", content_hash="old-hash")
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        current_version_id="task-1-v3",
    )
    session = _Session(task=task, current=version)
    storage = _Storage()
    monkeypatch.setattr(tasks, "get_session", _session_context(session))
    monkeypatch.setattr(tasks, "get_storage_client", lambda: storage)

    with pytest.raises(HTTPException, match="selected task version changed"):
        await tasks.complete_task_upload(
            task_id="task-1",
            task_name="task",
            version=2,
            content_hash="new-hash",
            org_id="org-1",
            overwrite_current_version=True,
            staging_key=f"task-upload-staging/task-1/{'b' * 32}.tar.gz",
            overwrite_base_content_hash="old-hash",
        )

    assert storage.copied == []


@pytest.mark.asyncio
async def test_complete_overwrite_commit_failure_does_not_replace_canonical_archive(
    monkeypatch,
) -> None:
    version = SimpleNamespace(
        id="task-1-v2",
        task_s3_key="tasks/task-1/v2/",
        content_hash="old-hash",
        message=None,
        expanded_at=None,
        expanded_manifest_key=None,
        pre_trial=None,
        pre_trial_status=None,
        pre_trial_error=None,
        pre_trial_started_at=None,
        pre_trial_finished_at=None,
    )
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        current_version_id=version.id,
    )

    class _FailingCommitSession(_Session):
        async def commit(self):
            raise RuntimeError("database unavailable")

    session = _FailingCommitSession(task=task, current=version)
    storage = _Storage()
    monkeypatch.setattr(tasks, "get_session", _session_context(session))
    monkeypatch.setattr(tasks, "get_storage_client", lambda: storage)
    monkeypatch.setattr(tasks.settings, "tasks_expand_archive", False)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await tasks.complete_task_upload(
            task_id="task-1",
            task_name="task",
            version=2,
            content_hash="new-hash",
            org_id="org-1",
            overwrite_current_version=True,
            staging_key=f"task-upload-staging/task-1/{'f' * 32}.tar.gz",
            overwrite_base_content_hash="old-hash",
        )

    assert storage.copied == [
        (
            f"task-upload-staging/task-1/{'f' * 32}.tar.gz",
            f"tasks/task-1/v2-revisions/{'f' * 32}/.oddish-task.tar.gz",
        )
    ]
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_complete_overwrite_replay_succeeds_after_staging_cleanup(
    monkeypatch,
) -> None:
    version = SimpleNamespace(
        id="task-1-v2",
        content_hash="new-hash",
        task_s3_key=f"tasks/task-1/v2-revisions/{'c' * 32}/",
    )
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        current_version_id=version.id,
    )
    session = _Session(task=task, current=version)
    storage = _Storage(exists=False)
    monkeypatch.setattr(tasks, "get_session", _session_context(session))
    monkeypatch.setattr(tasks, "get_storage_client", lambda: storage)

    result = await tasks.complete_task_upload(
        task_id="task-1",
        task_name="task",
        version=2,
        content_hash="new-hash",
        org_id="org-1",
        overwrite_current_version=True,
        staging_key=f"task-upload-staging/task-1/{'c' * 32}.tar.gz",
        overwrite_base_content_hash="old-hash",
    )

    assert result.existing_task is True
    assert storage.copied == []


@pytest.mark.asyncio
async def test_complete_overwrite_replay_succeeds_before_staging_cleanup(
    monkeypatch,
) -> None:
    version = SimpleNamespace(
        id="task-1-v2",
        content_hash="new-hash",
        task_s3_key=f"tasks/task-1/v2-revisions/{'c' * 32}/",
    )
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        current_version_id=version.id,
    )
    session = _Session(task=task, current=version)
    storage = _Storage()
    monkeypatch.setattr(tasks, "get_session", _session_context(session))
    monkeypatch.setattr(tasks, "get_storage_client", lambda: storage)

    result = await tasks.complete_task_upload(
        task_id="task-1",
        task_name="task",
        version=2,
        content_hash="new-hash",
        org_id="org-1",
        overwrite_current_version=True,
        staging_key=f"task-upload-staging/task-1/{'e' * 32}.tar.gz",
        overwrite_base_content_hash="old-hash",
    )

    assert result.existing_task is True
    assert result.s3_key == f"tasks/task-1/v2-revisions/{'c' * 32}/"
    assert storage.copied == []
    assert storage.deleted == [f"task-upload-staging/task-1/{'e' * 32}.tar.gz"]


@pytest.mark.asyncio
async def test_complete_overwrite_rejects_stale_staging_upload(monkeypatch) -> None:
    version = SimpleNamespace(id="task-1-v2", content_hash="newer-hash")
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        current_version_id=version.id,
    )
    session = _Session(task=task, current=version)
    storage = _Storage()
    monkeypatch.setattr(tasks, "get_session", _session_context(session))
    monkeypatch.setattr(tasks, "get_storage_client", lambda: storage)

    with pytest.raises(HTTPException, match="was overwritten during upload"):
        await tasks.complete_task_upload(
            task_id="task-1",
            task_name="task",
            version=2,
            content_hash="stale-hash",
            org_id="org-1",
            overwrite_current_version=True,
            staging_key=f"task-upload-staging/task-1/{'d' * 32}.tar.gz",
            overwrite_base_content_hash="old-hash",
        )

    assert storage.copied == []


@pytest.mark.asyncio
async def test_complete_overwrite_rejects_current_version_trial_history(
    monkeypatch,
) -> None:
    version = SimpleNamespace(id="task-1-v2", content_hash="old-hash")
    task = SimpleNamespace(
        id="task-1",
        org_id="org-1",
        current_version_id=version.id,
        verdict=None,
        verdict_status=None,
        verdict_started_at=None,
    )
    session = _Session(task=task, current=version, trial_count=1)
    storage = _Storage()
    monkeypatch.setattr(tasks, "get_session", _session_context(session))
    monkeypatch.setattr(tasks, "get_storage_client", lambda: storage)

    with pytest.raises(HTTPException, match="cannot be overwritten in place"):
        await tasks.complete_task_upload(
            task_id="task-1",
            task_name="task",
            version=2,
            content_hash="new-hash",
            org_id="org-1",
            overwrite_current_version=True,
            staging_key=f"task-upload-staging/task-1/{'9' * 32}.tar.gz",
            overwrite_base_content_hash="old-hash",
        )

    history_sql = str(session.scalar_queries[-1].compile())
    assert "trials.task_version_id" in history_sql
    assert storage.copied == []

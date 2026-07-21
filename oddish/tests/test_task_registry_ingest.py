from __future__ import annotations

from pathlib import Path
import sys

import pytest
import pytest_asyncio
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import tasks as tasks_api
from oddish.core.tasks import complete_task_upload, initialize_task_upload
from oddish.db.models import MetadataSource, TaskModel, TaskUploadEventModel, TaskVersionModel
from oddish.schemas import TaskMetadata, TaskProvenance


def _metadata() -> TaskMetadata:
    return TaskMetadata(
        description="Port CBACT01C.",
        category="ml-training",
        category_raw="ml_training",
        topic_tags=["compilers"],
        author_name="Ada",
        allow_internet=False,
        cpus=4,
        memory_mb=16384,
        gpus=1,
        gpu_types=["H100"],
    )


def _provenance() -> TaskProvenance:
    return TaskProvenance(
        source_repo="abundant-ai/harbor-lh",
        source_commit="a" * 40,
        source_ref="main",
        source_path="tasks/demo",
        uploader_is_ci=True,
        ci_provider="github_actions",
    )


class _FakeStorage:
    """No archive is really uploaded in these tests -- fake the S3 round trip
    so completion doesn't depend on a live bucket."""

    async def get_presigned_upload_url(self, *_args, **_kwargs) -> str:
        return "https://storage.example/upload"

    async def object_exists(self, *_args, **_kwargs) -> bool:
        return True


@pytest.fixture(autouse=True)
def _mock_storage(monkeypatch):
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: _FakeStorage())


_TEST_TASK_NAMES = (
    "registry-demo",
    "registry-noop",
    "registry-legacy",
    "registry-immutable",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_registry_tasks(session):
    """``initialize_task_upload``/``complete_task_upload`` commit through their
    own internal session, independent of the ``session`` fixture's rollback --
    clear residue from a prior run before each test so the hardcoded task
    names below stay safe to re-run."""
    await session.execute(
        TaskModel.__table__.delete().where(TaskModel.name.in_(_TEST_TASK_NAMES))
    )
    await session.commit()
    yield


@pytest.mark.asyncio
async def test_first_upload_persists_metadata_and_event(session):
    init = await initialize_task_upload(
        "registry-demo",
        content_hash="hash-v1",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    assert init.content_unchanged is False

    response = await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-demo",
        version=init.version,
        content_hash="hash-v1",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )

    task = (
        await session.execute(select(TaskModel).where(TaskModel.id == response.task_id))
    ).scalar_one()
    assert task.description == "Port CBACT01C."
    assert task.category == "ml-training"
    assert task.category_raw == "ml_training"
    assert task.metadata_source == MetadataSource.CLIENT

    events = (
        await session.execute(
            select(TaskUploadEventModel).where(
                TaskUploadEventModel.task_id == response.task_id
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].source_repo == "abundant-ai/harbor-lh"
    assert events[0].created_version is True


@pytest.mark.asyncio
async def test_content_unchanged_reupload_still_records(session):
    """The regression this whole table exists for.

    A no-op re-upload creates no version row, but must still update descriptive
    metadata and leave an audit trail of where it came from.
    """
    init = await initialize_task_upload(
        "registry-noop",
        content_hash="hash-same",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    first = await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-noop",
        version=init.version,
        content_hash="hash-same",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )

    changed = _metadata()
    changed.description = "Corrected description."
    second = await initialize_task_upload(
        "registry-noop",
        content_hash="hash-same",
        task_metadata=changed,
        provenance=TaskProvenance(source_repo="someone/laptop-copy"),
    )

    assert second.content_unchanged is True
    assert second.task_id == first.task_id

    task = (
        await session.execute(select(TaskModel).where(TaskModel.id == first.task_id))
    ).scalar_one()
    assert task.description == "Corrected description."

    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == first.task_id)
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    assert len(events) == 2
    assert events[1].task_version_id is None
    assert events[1].source_repo == "someone/laptop-copy"
    assert events[1].created_version is False


@pytest.mark.asyncio
async def test_upload_without_metadata_still_succeeds(session):
    """Old-CLI compatibility. If this breaks, all task uploads break."""
    init = await initialize_task_upload("registry-legacy", content_hash="hash-legacy")
    response = await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-legacy",
        version=init.version,
        content_hash="hash-legacy",
        register=True,
    )
    assert response.task_id

    task = (
        await session.execute(select(TaskModel).where(TaskModel.id == response.task_id))
    ).scalar_one()
    assert task.metadata_source is None
    assert task.description is None


@pytest.mark.asyncio
async def test_metadata_only_edit_does_not_mutate_existing_version(session):
    """task_versions rows are immutable.

    A trial records the task_version_id it ran under. If a later metadata-only
    edit rewrote that version's snapshot columns, the trial's recorded version
    would no longer describe what actually ran.
    """
    init = await initialize_task_upload(
        "registry-immutable",
        content_hash="hash-fixed",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    first = await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-immutable",
        version=init.version,
        content_hash="hash-fixed",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    version = (
        await session.execute(
            select(TaskVersionModel).where(TaskVersionModel.id == first.version_id)
        )
    ).scalar_one()
    original_snapshot = version.description_snapshot

    changed = _metadata()
    changed.description = "Rewritten after the fact."
    await initialize_task_upload(
        "registry-immutable",
        content_hash="hash-fixed",
        task_metadata=changed,
        provenance=_provenance(),
    )

    await session.refresh(version)
    assert version.description_snapshot == original_snapshot

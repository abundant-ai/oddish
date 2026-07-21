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
    "registry-reupload",
    "registry-retry-dedupe",
    "registry-retry-distinct-provenance",
    "registry-complete-retry-newtask",
    "registry-complete-retry-newversion",
    "registry-dbapierror",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_registry_tasks(session):
    """``initialize_task_upload``/``complete_task_upload`` commit through their
    own internal session, independent of the ``session`` fixture's rollback --
    clear residue from a prior run both before AND after each test so the
    hardcoded task names below stay safe to re-run and don't leak permanently."""
    await session.execute(
        TaskModel.__table__.delete().where(TaskModel.name.in_(_TEST_TASK_NAMES))
    )
    await session.commit()
    yield
    await session.execute(
        TaskModel.__table__.delete().where(TaskModel.name.in_(_TEST_TASK_NAMES))
    )
    await session.commit()


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


@pytest.mark.asyncio
async def test_reupload_existing_task_changed_content_cuts_new_version(session):
    """``complete_task_upload``'s ``existing_task is not None`` branch
    (core/tasks.py:390-455) had zero coverage. A changed-content re-upload
    must cut v2, update the task's descriptive columns, snapshot the new
    version, leave v1 untouched, and record a created_version=True event.
    """
    init1 = await initialize_task_upload(
        "registry-reupload",
        content_hash="hash-v1",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    first = await complete_task_upload(
        task_id=init1.task_id,
        task_name="registry-reupload",
        version=init1.version,
        content_hash="hash-v1",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    v1 = (
        await session.execute(
            select(TaskVersionModel).where(TaskVersionModel.id == first.version_id)
        )
    ).scalar_one()
    original_snapshot = v1.description_snapshot

    changed = _metadata()
    changed.description = "Rewritten for v2."
    changed.category = "systems"
    changed.cpus = 8

    init2 = await initialize_task_upload(
        "registry-reupload",
        content_hash="hash-v2",
        task_metadata=changed,
        provenance=_provenance(),
    )
    assert init2.content_unchanged is False
    assert init2.version == 2

    second = await complete_task_upload(
        task_id=init2.task_id,
        task_name="registry-reupload",
        version=init2.version,
        content_hash="hash-v2",
        register=True,
        task_metadata=changed,
        provenance=_provenance(),
    )
    assert second.version == 2
    assert second.version_id != first.version_id

    # ``TaskVersionModel.task`` is ``lazy="selectin"``, so the ``v1`` fetch
    # above already cached a (now stale) ``TaskModel`` in this session's
    # identity map -- force a refresh rather than reading the cached copy.
    task = (
        await session.execute(
            select(TaskModel)
            .where(TaskModel.id == first.task_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert task.description == "Rewritten for v2."
    assert task.category == "systems"

    v2 = (
        await session.execute(
            select(TaskVersionModel).where(TaskVersionModel.id == second.version_id)
        )
    ).scalar_one()
    assert v2.description_snapshot == "Rewritten for v2."
    assert v2.allow_internet == changed.allow_internet
    assert v2.cpus == 8

    await session.refresh(v1)
    assert v1.description_snapshot == original_snapshot

    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == first.task_id)
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    assert len(events) == 2
    assert events[1].created_version is True
    assert events[1].task_version_id == second.version_id


@pytest.mark.asyncio
async def test_retried_content_unchanged_init_collapses_to_one_event(session):
    """A client retry (transient 5xx / lost response) of the SAME init
    request must not duplicate the append-only audit row -- this is the
    exact scenario cli/api.py's _retry_request wraps around init."""
    init = await initialize_task_upload(
        "registry-retry-dedupe",
        content_hash="hash-retry",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    first = await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-retry-dedupe",
        version=init.version,
        content_hash="hash-retry",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )

    # Two content-unchanged calls, identical provenance, back to back --
    # simulates the client retrying after not seeing the first response.
    await initialize_task_upload(
        "registry-retry-dedupe",
        content_hash="hash-retry",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    await initialize_task_upload(
        "registry-retry-dedupe",
        content_hash="hash-retry",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )

    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == first.task_id)
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    # One created_version=True event from the real upload, plus exactly one
    # collapsed content-unchanged event -- not three.
    assert len(events) == 2
    assert events[1].created_version is False
    assert events[1].task_version_id is None


@pytest.mark.asyncio
async def test_content_unchanged_different_source_commit_is_not_deduped(session):
    """A genuinely distinct re-upload (different provenance) must still get
    its own row even though the content hash matches -- only true retries of
    the SAME request collapse."""
    init = await initialize_task_upload(
        "registry-retry-distinct-provenance",
        content_hash="hash-distinct",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    first = await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-retry-distinct-provenance",
        version=init.version,
        content_hash="hash-distinct",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )

    await initialize_task_upload(
        "registry-retry-distinct-provenance",
        content_hash="hash-distinct",
        task_metadata=_metadata(),
        provenance=TaskProvenance(source_repo="abundant-ai/harbor-lh", source_commit="b" * 40),
    )
    await initialize_task_upload(
        "registry-retry-distinct-provenance",
        content_hash="hash-distinct",
        task_metadata=_metadata(),
        provenance=TaskProvenance(source_repo="abundant-ai/harbor-lh", source_commit="c" * 40),
    )

    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == first.task_id)
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    assert len(events) == 3
    assert [e.source_commit for e in events] == [_provenance().source_commit, "b" * 40, "c" * 40]


@pytest.mark.asyncio
async def test_retried_complete_new_task_collapses_to_one_event(session):
    """I-2 (b): a retried /tasks/upload/complete for a brand-new task must
    not duplicate the audit event.

    On retry, ``existing_task`` is no longer None (the first call already
    committed it), so the function falls into the "existing task" branch
    with ``new_version_created=False`` -- a *different* created_version value
    than the original call recorded, which is exactly why record_upload_event
    must not key its dedupe on created_version."""
    init = await initialize_task_upload(
        "registry-complete-retry-newtask",
        content_hash="hash-a",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    first = await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-complete-retry-newtask",
        version=init.version,
        content_hash="hash-a",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )

    # Simulates the CLI's _retry_request re-POSTing the identical complete
    # body after a transient 5xx / lost response.
    retry = await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-complete-retry-newtask",
        version=init.version,
        content_hash="hash-a",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    assert retry.task_id == first.task_id

    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == first.task_id)
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].created_version is True

    # A genuinely distinct complete call (different provenance) for the SAME
    # version must still get its own row.
    await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-complete-retry-newtask",
        version=init.version,
        content_hash="hash-a",
        register=True,
        task_metadata=_metadata(),
        provenance=TaskProvenance(source_repo="abundant-ai/harbor-lh", source_commit="d" * 40),
    )
    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == first.task_id)
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    assert len(events) == 2


@pytest.mark.asyncio
async def test_retried_complete_new_version_collapses_to_one_event(session):
    """I-2 (b): a retried /tasks/upload/complete cutting a new version on an
    EXISTING task must not duplicate the audit event either."""
    init1 = await initialize_task_upload(
        "registry-complete-retry-newversion",
        content_hash="hash-v1",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    await complete_task_upload(
        task_id=init1.task_id,
        task_name="registry-complete-retry-newversion",
        version=init1.version,
        content_hash="hash-v1",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )

    init2 = await initialize_task_upload(
        "registry-complete-retry-newversion",
        content_hash="hash-v2",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    assert init2.version == 2
    first2 = await complete_task_upload(
        task_id=init2.task_id,
        task_name="registry-complete-retry-newversion",
        version=init2.version,
        content_hash="hash-v2",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )

    # Same retry scenario as above, but for the v2-cutting call: the retry
    # finds v2 already committed, so new_version_created flips to False.
    retry2 = await complete_task_upload(
        task_id=init2.task_id,
        task_name="registry-complete-retry-newversion",
        version=init2.version,
        content_hash="hash-v2",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    assert retry2.version_id == first2.version_id

    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == first2.task_id)
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    # v1's created event + v2's created event -- the retry collapsed, not a
    # third row.
    assert len(events) == 2
    assert events[1].task_version_id == first2.version_id
    assert events[1].created_version is True

    # A genuinely distinct complete call for the same v2 (different
    # provenance) must still get its own row.
    await complete_task_upload(
        task_id=init2.task_id,
        task_name="registry-complete-retry-newversion",
        version=init2.version,
        content_hash="hash-v2",
        register=True,
        task_metadata=_metadata(),
        provenance=TaskProvenance(source_repo="abundant-ai/harbor-lh", source_commit="e" * 40),
    )
    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == first2.task_id)
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    assert len(events) == 3


@pytest.mark.asyncio
async def test_content_unchanged_survives_derived_tag_db_error(session, monkeypatch):
    """C-1 merge blocker: a DBAPIError inside rebuild_derived_tags (e.g. the
    native ``tag_assignment_source`` enum missing 'DERIVED' because the
    migration hasn't landed yet) must not fail the upload or poison the
    transaction. The task's descriptive columns and the upload event row
    must still be written -- only the derived tags are left stale.
    """
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    from oddish.core.tags import derived as derived_module
    from oddish.db.models import TagAssignmentModel, TagAssignmentSource, TagAssignmentState
    from oddish.db.models import TagModel

    init = await initialize_task_upload(
        "registry-dbapierror",
        content_hash="hash-same",
        task_metadata=_metadata(),
        provenance=_provenance(),
    )
    first = await complete_task_upload(
        task_id=init.task_id,
        task_name="registry-dbapierror",
        version=init.version,
        content_hash="hash-same",
        register=True,
        task_metadata=_metadata(),
        provenance=_provenance(),
    )

    real_execute = AsyncSession.execute

    async def _boom_on_derived_read(self, statement, *args, **kwargs):
        if statement is derived_module._EXISTING_NON_DERIVED:
            raise DBAPIError(
                "SELECT tag_id FROM tag_assignments ...",
                {},
                Exception(
                    'invalid input value for enum tag_assignment_source: "DERIVED"'
                ),
            )
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _boom_on_derived_read)

    changed = _metadata()
    changed.description = "Updated despite tag DB failure."
    changed.category = "systems"
    second = await initialize_task_upload(
        "registry-dbapierror",
        content_hash="hash-same",
        task_metadata=changed,
        provenance=TaskProvenance(source_repo="someone/laptop-copy"),
    )

    assert second.content_unchanged is True
    assert second.task_id == first.task_id

    task = (
        await session.execute(select(TaskModel).where(TaskModel.id == first.task_id))
    ).scalar_one()
    assert task.description == "Updated despite tag DB failure."
    assert task.category == "systems"

    events = (
        await session.execute(
            select(TaskUploadEventModel)
            .where(TaskUploadEventModel.task_id == first.task_id)
            .order_by(TaskUploadEventModel.created_at)
        )
    ).scalars().all()
    assert len(events) == 2
    assert events[1].source_repo == "someone/laptop-copy"

    active = (
        await session.execute(
            select(TagModel.normalized_value)
            .join(TagAssignmentModel, TagAssignmentModel.tag_id == TagModel.id)
            .where(
                TagAssignmentModel.task_id == first.task_id,
                TagAssignmentModel.source == TagAssignmentSource.DERIVED,
                TagAssignmentModel.state == TagAssignmentState.ACTIVE,
                TagModel.normalized_key == "category",
            )
        )
    ).scalars().all()
    # The rebuild never ran -- the OLD category tag (ml-training) is still
    # the only active DERIVED category tag; "systems" was never assigned.
    assert active == ["ml-training"]

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.tags.derived import derived_tag_pairs
from oddish.db.models import TagAssignmentSource
from oddish.schemas import TaskMetadata


def test_derived_source_enum_exists():
    assert TagAssignmentSource.DERIVED.value == "DERIVED"


def test_derived_tag_pairs_maps_category_topics_and_org():
    metadata = TaskMetadata(
        category="ml-training",
        topic_tags=["compilers", "rust"],
        author_organization="Abundant AI",
    )
    assert derived_tag_pairs(metadata) == [
        ("category", "ml-training"),
        ("topic", "compilers"),
        ("topic", "rust"),
        ("org", "abundant-ai"),
    ]


def test_derived_tag_pairs_skips_missing_fields():
    assert derived_tag_pairs(TaskMetadata()) == []


@pytest.mark.asyncio
async def test_rebuild_replaces_rather_than_accumulates(session):
    """Recategorizing must not leave the old category matching forever."""
    from oddish.core.tags.derived import rebuild_derived_tags
    from oddish.db.models import TagAssignmentModel, TagAssignmentState

    await rebuild_derived_tags(
        session,
        task_id="t-1",
        org_id=None,
        metadata=TaskMetadata(category="ml-training"),
    )
    await rebuild_derived_tags(
        session,
        task_id="t-1",
        org_id=None,
        metadata=TaskMetadata(category="optimization"),
    )

    active = (
        await session.execute(
            select(TagAssignmentModel).where(
                TagAssignmentModel.task_id == "t-1",
                TagAssignmentModel.source == TagAssignmentSource.DERIVED,
                TagAssignmentModel.state == TagAssignmentState.ACTIVE,
            )
        )
    ).scalars().all()
    assert len(active) == 1


@pytest.mark.asyncio
async def test_rebuild_does_not_steal_a_direct_assignment(session):
    """A DIRECT assignment must survive derivation of the same tag.

    tag_assignments is unique on (org_id, tag_id, scope, target_id) with source
    OUTSIDE the key, so a naive upsert would flip the human's row to DERIVED and
    the next rebuild would retract it.
    """
    from oddish.core.tags.derived import rebuild_derived_tags
    from oddish.core.tags.service import assign_tag_core, create_tag_core
    from oddish.db.models import TagAssignmentModel, TagAssignmentState

    tag_id = await create_tag_core(
        session,
        key="category",
        value="ml-training",
        org_id=None,
        actor_user_id="u1",
        policy={},
        is_admin=True,
    )
    await assign_tag_core(
        session, tag_id=tag_id, scope="TASK", target_id="t-3", task_id="t-3",
        org_id=None, actor_user_id="u1", source="DIRECT",
    )

    await rebuild_derived_tags(
        session, task_id="t-3", org_id=None,
        metadata=TaskMetadata(category="ml-training"),
    )

    row = (
        await session.execute(
            select(TagAssignmentModel).where(
                TagAssignmentModel.target_id == "t-3",
                TagAssignmentModel.tag_id == tag_id,
            )
        )
    ).scalar_one()
    assert row.source == TagAssignmentSource.DIRECT
    assert row.state == TagAssignmentState.ACTIVE
    # assign_tag_core's ON CONFLICT DO UPDATE sets assigned_by_user_id from
    # EXCLUDED unconditionally (only `source` is CASE-protected against
    # DIRECT), so a rebuild that skips this guard silently reassigns a
    # human's row to the system actor even though source looks untouched.
    assert row.assigned_by_user_id == "u1"


@pytest.mark.asyncio
async def test_rebuild_does_not_resurrect_a_removed_direct_assignment(session):
    """A REMOVED DIRECT assignment must stay retractable forever.

    If a human previously removed their DIRECT category tag, a rebuild that
    only excludes ACTIVE non-DERIVED rows would upsert it back to ACTIVE with
    source='DIRECT' (assign_tag_core's CASE). _RETRACT_STALE_DERIVED only
    touches source='DERIVED' rows, so that resurrected row could then never
    be retracted again -- the exact failure TASK scope was chosen to prevent.
    """
    from oddish.core.tags.derived import rebuild_derived_tags
    from oddish.core.tags.service import assign_tag_core, create_tag_core
    from oddish.db.models import TagAssignmentModel, TagAssignmentState

    tag_id = await create_tag_core(
        session,
        key="category",
        value="ml-training",
        org_id=None,
        actor_user_id="u1",
        policy={},
        is_admin=True,
    )
    await assign_tag_core(
        session, tag_id=tag_id, scope="TASK", target_id="t-4", task_id="t-4",
        org_id=None, actor_user_id="u1", source="DIRECT",
    )
    # Human removes the DIRECT assignment; the row stays, marked REMOVED.
    await session.execute(
        TagAssignmentModel.__table__.update()
        .where(
            TagAssignmentModel.tag_id == tag_id,
            TagAssignmentModel.target_id == "t-4",
        )
        .values(state=TagAssignmentState.REMOVED)
    )

    await rebuild_derived_tags(
        session, task_id="t-4", org_id=None,
        metadata=TaskMetadata(category="ml-training"),
    )

    row = (
        await session.execute(
            select(TagAssignmentModel).where(
                TagAssignmentModel.target_id == "t-4",
                TagAssignmentModel.tag_id == tag_id,
            )
        )
    ).scalar_one()
    # Still REMOVED and still DIRECT -- DERIVED must not have touched it.
    assert row.state == TagAssignmentState.REMOVED
    assert row.source == TagAssignmentSource.DIRECT


@pytest.mark.asyncio
async def test_rebuild_retracts_stale_and_keeps_good_pairs_when_one_pair_fails(session):
    """M-4: a partial failure mid-rebuild must not leave stale assignments.

    Same scenario as test_task_upload_routes.py's C3 (a profane topic tag),
    but at the rebuild_derived_tags unit level: the old category must still
    be retracted and the new, valid category applied, even though the
    profane topic pair is skipped. Before this fix, create_tag_core raising
    on the profane pair aborted the whole function before retraction ever
    ran, so the OLD category stayed ACTIVE forever and the new category tag
    (already created before the failing pair) was committed with no
    assignment.
    """
    from oddish.core.tags.derived import rebuild_derived_tags
    from oddish.db.models import TagAssignmentModel, TagAssignmentState
    from oddish.db.models import TagModel

    await rebuild_derived_tags(
        session,
        task_id="t-5",
        org_id=None,
        metadata=TaskMetadata(category="ml-training"),
    )

    # Second rebuild: a good category swap plus a profane topic tag that
    # must fail create_tag_core's real profanity check.
    await rebuild_derived_tags(
        session,
        task_id="t-5",
        org_id=None,
        metadata=TaskMetadata(category="optimization", topic_tags=["fuck"]),
    )

    active = (
        await session.execute(
            select(TagAssignmentModel)
            .join(TagModel, TagModel.id == TagAssignmentModel.tag_id)
            .where(
                TagAssignmentModel.task_id == "t-5",
                TagAssignmentModel.source == TagAssignmentSource.DERIVED,
                TagAssignmentModel.state == TagAssignmentState.ACTIVE,
            )
            .add_columns(TagModel.normalized_key, TagModel.normalized_value)
        )
    ).all()
    active_pairs = {(row.normalized_key, row.normalized_value) for row in active}
    # The old category is retracted, the new one is applied, and the profane
    # topic never made it into the ACTIVE set at all.
    assert active_pairs == {("category", "optimization")}

    # No orphan tags row exists for the skipped profane pair.
    profane_tags = (
        await session.execute(
            select(TagModel).where(
                TagModel.normalized_key == "topic",
                TagModel.normalized_value == "fuck",
            )
        )
    ).scalars().all()
    assert profane_tags == []


@pytest.mark.asyncio
async def test_rebuild_recomputes_projection_exactly_once(session):
    """assign_tag_core's per-assignment recompute must be suppressed for the
    derived batch -- only rebuild_derived_tags' trailing recompute should run,
    or an N-tag rebuild pays an O(tags x versions) CTE per assignment.
    """
    import oddish.core.tags.derived as derived_module
    import oddish.core.tags.service as service_module
    from oddish.core.tags.derived import rebuild_derived_tags
    from oddish.db.models import TagAssignmentModel, TagAssignmentState, TaskModel, TaskVersionModel

    task = TaskModel(
        name="derived-recompute-once", org_id=None, user="tester",
        task_path="s3://tasks/derived-recompute-once",
    )
    session.add(task)
    await session.flush()
    version = TaskVersionModel(
        id=f"{task.id}-v1", task_id=task.id, version=1, task_path=task.task_path
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    await session.flush()

    # assign_tag_core (service.py) and rebuild_derived_tags (derived.py) each
    # hold their own `from .projection import recompute_task_browse_projection`
    # binding, so both must be patched to count every call site.
    calls = []
    real_recompute = derived_module.recompute_task_browse_projection

    async def _spy(session, *, task_id):
        calls.append(task_id)
        return await real_recompute(session, task_id=task_id)

    derived_module.recompute_task_browse_projection = _spy
    service_module.recompute_task_browse_projection = _spy
    try:
        await rebuild_derived_tags(
            session,
            task_id=task.id,
            org_id=None,
            metadata=TaskMetadata(
                category="ml-training",
                topic_tags=["compilers", "rust"],
            ),
        )
    finally:
        derived_module.recompute_task_browse_projection = real_recompute
        service_module.recompute_task_browse_projection = real_recompute

    assert len(calls) == 1

    active = (
        await session.execute(
            select(TagAssignmentModel).where(
                TagAssignmentModel.task_id == task.id,
                TagAssignmentModel.source == TagAssignmentSource.DERIVED,
                TagAssignmentModel.state == TagAssignmentState.ACTIVE,
            )
        )
    ).scalars().all()
    assert len(active) == 3

    await session.refresh(task)
    assert set(task.effective_tag_ids or []) == {row.tag_id for row in active}

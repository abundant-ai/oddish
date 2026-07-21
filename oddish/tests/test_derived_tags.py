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

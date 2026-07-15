import uuid

import pytest

from oddish.db.models import (
    CapabilityCategoryModel,
    CapabilityCategoryTagModel,
    CapabilityModel,
    utcnow,
)
from oddish.db.taxonomy_query import load_taxonomy


def _sfx() -> str:
    # Shared Postgres: 'verification' and 'agent-early-stop' are seed rows.
    return uuid.uuid4().hex[:6]


@pytest.mark.asyncio
async def test_load_taxonomy_builds_primary_and_extra_tags(session):
    # captax_001 seeds real rows with these slugs as PKs on any migrated DB;
    # uniquify so this test's inserts never collide with the seed data.
    suffix = uuid.uuid4().hex[:8]
    verification_slug = f"verification-{suffix}"
    tool_slug = f"tool-{suffix}"
    early_stop_slug = f"agent-early-stop-{suffix}"
    tool_selection_slug = f"tool-selection-error-{suffix}"

    session.add_all([
        CapabilityCategoryModel(slug=verification_slug, name="Verification failures",
                                description="d", sort_order=0),
        CapabilityCategoryModel(slug=tool_slug, name="Tool failures",
                                description="d", sort_order=1),
        CapabilityModel(slug=early_stop_slug, name="Agent Early Stop",
                        description="Stops early.", example="apex-swe."),
        CapabilityModel(slug=tool_selection_slug, name="Tool Selection Error",
                        description="Wrong tool.", example="curl over MCP."),
    ])
    await session.flush()
    session.add_all([
        CapabilityCategoryTagModel(capability_slug=early_stop_slug,
                                   category_slug=verification_slug, is_primary=True),
        CapabilityCategoryTagModel(capability_slug=tool_selection_slug,
                                   category_slug=tool_slug, is_primary=True),
        CapabilityCategoryTagModel(capability_slug=tool_selection_slug,
                                   category_slug=verification_slug, is_primary=False),
    ])
    await session.flush()

    tax = await load_taxonomy(session)

    our_category_slugs = {verification_slug, tool_slug}
    our_categories = [c for c in tax.categories if c.slug in our_category_slugs]
    assert [c.slug for c in our_categories] == [verification_slug, tool_slug]

    by_slug = {c.slug: c for c in tax.capabilities}
    assert by_slug[early_stop_slug].primary_category == verification_slug
    assert by_slug[early_stop_slug].extra_categories == ()
    assert by_slug[tool_selection_slug].primary_category == tool_slug
    assert by_slug[tool_selection_slug].extra_categories == (verification_slug,)


@pytest.mark.asyncio
async def test_load_taxonomy_skips_untagged_capability(session):
    """A capability with no primary tag cannot be grouped, so it must not reach
    the rubric -- it would render under no category and be unpickable."""
    orphan_slug = f"orphan-{uuid.uuid4().hex[:8]}"
    session.add(CapabilityModel(slug=orphan_slug, name="Orphan", description="d"))
    await session.flush()
    tax = await load_taxonomy(session)
    assert orphan_slug not in {c.slug for c in tax.capabilities}


@pytest.mark.asyncio
async def test_load_taxonomy_skips_capability_whose_primary_category_is_soft_deleted(
    session,
):
    """A soft-deleted category is filtered out of cat_rows (session-level soft
    delete), but the tag row and the capability row survive -- without this
    guard the capability would render with no live category to group under."""
    s = _sfx()
    cat_slug = f"cat-{s}"
    cap_slug = f"cap-{s}"
    session.add_all([
        CapabilityCategoryModel(slug=cat_slug, name="Doomed", description="d",
                                sort_order=0, deleted_at=utcnow()),
        CapabilityModel(slug=cap_slug, name="Orphaned by Deletion",
                        description="d"),
    ])
    await session.flush()
    session.add(CapabilityCategoryTagModel(
        capability_slug=cap_slug, category_slug=cat_slug, is_primary=True,
    ))
    await session.flush()

    tax = await load_taxonomy(session)

    assert cat_slug not in {c.slug for c in tax.categories}
    assert cap_slug not in {c.slug for c in tax.capabilities}


@pytest.mark.asyncio
async def test_load_taxonomy_drops_extra_tag_pointing_at_soft_deleted_category(
    session,
):
    """A non-primary tag naming a soft-deleted category must not surface as a
    live cross-reference in extra_categories."""
    s = _sfx()
    live_cat = f"live-{s}"
    dead_cat = f"dead-{s}"
    cap_slug = f"cap-{s}"
    session.add_all([
        CapabilityCategoryModel(slug=live_cat, name="Live", description="d",
                                sort_order=0),
        CapabilityCategoryModel(slug=dead_cat, name="Dead", description="d",
                                sort_order=1, deleted_at=utcnow()),
        CapabilityModel(slug=cap_slug, name="Multi-tagged", description="d"),
    ])
    await session.flush()
    session.add_all([
        CapabilityCategoryTagModel(capability_slug=cap_slug, category_slug=live_cat,
                                   is_primary=True),
        CapabilityCategoryTagModel(capability_slug=cap_slug, category_slug=dead_cat,
                                   is_primary=False),
    ])
    await session.flush()

    tax = await load_taxonomy(session)

    by_slug = {c.slug: c for c in tax.capabilities}
    assert by_slug[cap_slug].primary_category == live_cat
    assert by_slug[cap_slug].extra_categories == ()

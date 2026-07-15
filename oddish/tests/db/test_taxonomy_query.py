import pytest

from oddish.db.models import (
    CapabilityCategoryModel,
    CapabilityCategoryTagModel,
    CapabilityModel,
)
from oddish.db.taxonomy_query import load_taxonomy


@pytest.mark.asyncio
async def test_load_taxonomy_builds_primary_and_extra_tags(session):
    session.add_all([
        CapabilityCategoryModel(slug="verification", name="Verification failures",
                                description="d", sort_order=0),
        CapabilityCategoryModel(slug="tool", name="Tool failures",
                                description="d", sort_order=1),
        CapabilityModel(slug="agent-early-stop", name="Agent Early Stop",
                        description="Stops early.", example="apex-swe."),
        CapabilityModel(slug="tool-selection-error", name="Tool Selection Error",
                        description="Wrong tool.", example="curl over MCP."),
    ])
    await session.flush()
    session.add_all([
        CapabilityCategoryTagModel(capability_slug="agent-early-stop",
                                   category_slug="verification", is_primary=True),
        CapabilityCategoryTagModel(capability_slug="tool-selection-error",
                                   category_slug="tool", is_primary=True),
        CapabilityCategoryTagModel(capability_slug="tool-selection-error",
                                   category_slug="verification", is_primary=False),
    ])
    await session.flush()

    tax = await load_taxonomy(session)

    assert [c.slug for c in tax.categories] == ["verification", "tool"]
    by_slug = {c.slug: c for c in tax.capabilities}
    assert by_slug["agent-early-stop"].primary_category == "verification"
    assert by_slug["agent-early-stop"].extra_categories == ()
    assert by_slug["tool-selection-error"].primary_category == "tool"
    assert by_slug["tool-selection-error"].extra_categories == ("verification",)


@pytest.mark.asyncio
async def test_load_taxonomy_skips_untagged_capability(session):
    """A capability with no primary tag cannot be grouped, so it must not reach
    the rubric -- it would render under no category and be unpickable."""
    session.add(CapabilityModel(slug="orphan", name="Orphan", description="d"))
    await session.flush()
    tax = await load_taxonomy(session)
    assert [c.slug for c in tax.capabilities] == []

import uuid

import pytest

from oddish.db.models import (
    CapabilityCategoryModel,
    CapabilityCategoryTagModel,
    CapabilityModel,
    CapabilityProposalModel,
)
from scripts.promote_capability import promote, reject  # backend/ is the import root


def _sfx() -> str:
    # Shared Postgres: 'verification' and 'hypothesis-fixation' are seed rows.
    return uuid.uuid4().hex[:6]


@pytest.mark.asyncio
async def test_promote_creates_capability_and_primary_tag(session):
    s = _sfx()
    cat, slug, pid = f"verif-{s}", f"hypfix-{s}", f"p1-{s}"
    session.add(CapabilityCategoryModel(slug=cat, name="V", sort_order=0))
    session.add(CapabilityProposalModel(
        id=pid, slug_suggestion=slug, name="Hypothesis Fixation",
        description="d", example="e", category_slugs=[cat],
        analyzer_id=f"an-{s}", trial_ids=["good-1"], status="PENDING"))
    await session.flush()

    await promote(session, pid, primary_category=cat)

    cap = await session.get(CapabilityModel, slug)
    assert cap is not None and cap.name == "Hypothesis Fixation"
    tag = await session.get(CapabilityCategoryTagModel, (slug, cat))
    assert tag.is_primary is True
    prop = await session.get(CapabilityProposalModel, pid)
    assert prop.status == "PROMOTED"
    assert prop.promoted_capability_slug == slug


@pytest.mark.asyncio
async def test_reject_with_merge_target_records_the_survivor(session):
    """Findings citing the rejected slug resolve to the merge target -- that is
    the whole reason capability_slug is not an FK."""
    s = _sfx()
    slug, pid, survivor = f"early-{s}", f"p2-{s}", f"agent-early-stop-{s}"
    session.add(CapabilityProposalModel(
        id=pid, slug_suggestion=slug, name="Early Stop", description="d",
        analyzer_id=f"an-{s}", status="PENDING"))
    await session.flush()

    await reject(session, pid, merge_into=survivor)

    prop = await session.get(CapabilityProposalModel, pid)
    assert prop.status == "REJECTED"
    assert prop.promoted_capability_slug == survivor
    # Rejection must not mint a capability for the rejected slug.
    assert await session.get(CapabilityModel, slug) is None

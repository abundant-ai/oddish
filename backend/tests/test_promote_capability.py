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


@pytest.mark.asyncio
async def test_promote_idempotent_with_same_primary_category(session):
    """Re-running promote() with the same primary category must be idempotent.
    Tests that both the CapabilityModel and primary CapabilityCategoryTagModel
    guards prevent duplicates on re-run."""
    s = _sfx()
    cat, slug, pid = f"verif-{s}", f"hypfix-{s}", f"p3-{s}"
    session.add(CapabilityCategoryModel(slug=cat, name="V", sort_order=0))
    session.add(CapabilityProposalModel(
        id=pid, slug_suggestion=slug, name="Hypothesis Fixation",
        description="d", example="e", category_slugs=[cat],
        analyzer_id=f"an-{s}", trial_ids=["good-1"], status="PENDING"))
    await session.flush()

    # First promote call.
    await promote(session, pid, primary_category=cat)
    cap_1 = await session.get(CapabilityModel, slug)
    tag_1 = await session.get(CapabilityCategoryTagModel, (slug, cat))
    assert cap_1 is not None
    assert tag_1.is_primary is True

    # Reset proposal status so we can call promote again (simulates retry).
    prop = await session.get(CapabilityProposalModel, pid)
    prop.status = "PENDING"
    prop.promoted_capability_slug = None
    prop.reviewed_at = None
    prop.reviewed_by = None
    await session.flush()

    # Second promote call with identical arguments must not raise.
    await promote(session, pid, primary_category=cat)

    # Verify end state is correct: exactly one tag row for (slug, cat).
    cap_2 = await session.get(CapabilityModel, slug)
    tag_2 = await session.get(CapabilityCategoryTagModel, (slug, cat))
    assert cap_2 is not None and cap_2.name == "Hypothesis Fixation"
    assert tag_2.is_primary is True


@pytest.mark.asyncio
async def test_promote_idempotent_with_extra_categories(session):
    """Re-running promote() with extra categories must be idempotent.
    Tests that the extra category CapabilityCategoryTagModel guard prevents
    duplicates on re-run."""
    s = _sfx()
    primary_cat, extra_cat = f"verif-{s}", f"extra-{s}"
    slug, pid = f"hypfix-{s}", f"p4-{s}"
    session.add(CapabilityCategoryModel(slug=primary_cat, name="Primary", sort_order=0))
    session.add(CapabilityCategoryModel(slug=extra_cat, name="Extra", sort_order=1))
    session.add(CapabilityProposalModel(
        id=pid, slug_suggestion=slug, name="Hypothesis Fixation",
        description="d", example="e", category_slugs=[primary_cat, extra_cat],
        analyzer_id=f"an-{s}", trial_ids=["good-1"], status="PENDING"))
    await session.flush()

    # First promote call with extra category.
    await promote(session, pid, primary_category=primary_cat, extra_categories=(extra_cat,))
    tag_extra_1 = await session.get(CapabilityCategoryTagModel, (slug, extra_cat))
    assert tag_extra_1 is not None and tag_extra_1.is_primary is False

    # Reset proposal status for retry.
    prop = await session.get(CapabilityProposalModel, pid)
    prop.status = "PENDING"
    prop.promoted_capability_slug = None
    prop.reviewed_at = None
    prop.reviewed_by = None
    await session.flush()

    # Second promote call with identical arguments must not raise.
    await promote(session, pid, primary_category=primary_cat, extra_categories=(extra_cat,))

    # Verify end state: exactly one tag row for (slug, extra_cat) with is_primary=False.
    tag_extra_2 = await session.get(CapabilityCategoryTagModel, (slug, extra_cat))
    assert tag_extra_2 is not None and tag_extra_2.is_primary is False

"""The single DB -> Taxonomy reader.

Everything downstream (prompt rendering, snapshotting) takes a Taxonomy value,
so this is the only place that needs a session. Keeping it alone here is what
lets prompt_builder stay pure.
"""

from __future__ import annotations

from sqlalchemy import select

from oddish.db.models import (
    CapabilityCategoryModel,
    CapabilityCategoryTagModel,
    CapabilityModel,
)
from oddish.evals.analyzer.taxonomy import Capability, Category, Taxonomy


async def load_taxonomy(session) -> Taxonomy:
    cat_rows = (
        await session.execute(
            select(CapabilityCategoryModel).order_by(
                CapabilityCategoryModel.sort_order, CapabilityCategoryModel.slug
            )
        )
    ).scalars().all()
    cap_rows = (
        await session.execute(select(CapabilityModel).order_by(CapabilityModel.slug))
    ).scalars().all()
    tag_rows = (
        await session.execute(select(CapabilityCategoryTagModel))
    ).scalars().all()

    # cat_rows is already soft-delete-filtered, but tag_rows (and the
    # capabilities they point at) are not -- a tag can still name a category
    # slug that no longer has a live row.
    live_category_slugs = {c.slug for c in cat_rows}

    primary: dict[str, str] = {}
    extra: dict[str, list[str]] = {}
    for t in tag_rows:
        if t.is_primary:
            primary[t.capability_slug] = t.category_slug
        else:
            extra.setdefault(t.capability_slug, []).append(t.category_slug)

    capabilities = tuple(
        Capability(
            slug=c.slug,
            name=c.name,
            description=c.description,
            example=c.example or "",
            primary_category=primary[c.slug],
            extra_categories=tuple(sorted(
                s for s in extra.get(c.slug, []) if s in live_category_slugs
            )),
        )
        # An untagged (or primary-soft-deleted) capability has no live group to
        # render under, so it would be invisible-but-pickable in the rubric.
        # Drop it rather than half-show it.
        for c in cap_rows
        if c.slug in primary and primary[c.slug] in live_category_slugs
    )
    return Taxonomy(
        categories=tuple(
            Category(slug=c.slug, name=c.name, description=c.description or "",
                     sort_order=c.sort_order)
            for c in cat_rows
        ),
        capabilities=capabilities,
    )

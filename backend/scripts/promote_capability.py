"""Review agent-authored capability proposals.

The human half of propose-and-promote. Nothing reaches the live rubric without
passing through here: parse_cohort_result overrides the model wherever a host
fact exists, but a proposal's content is model-authored by definition and has no
host fact to check against. This review is what stands in for that override.

Run from backend/ (pyproject sets pythonpath = ["."]):
    uv run python -m scripts.promote_capability list
    uv run python -m scripts.promote_capability promote p1 --category verification
    uv run python -m scripts.promote_capability reject p2 --merge-into agent-early-stop
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from oddish.db.connection import get_session
from oddish.db.models import (
    CapabilityCategoryTagModel,
    CapabilityModel,
    CapabilityProposalModel,
    utcnow,
)


async def list_pending(session) -> list[CapabilityProposalModel]:
    return list((await session.execute(
        select(CapabilityProposalModel)
        .where(CapabilityProposalModel.status == "PENDING")
        .order_by(CapabilityProposalModel.created_at)
    )).scalars().all())


async def promote(
    session, proposal_id: str, *, primary_category: str,
    extra_categories: tuple[str, ...] = (), slug: str | None = None,
    reviewed_by: str = "cli",
) -> str:
    prop = await session.get(CapabilityProposalModel, proposal_id)
    if prop is None:
        raise ValueError(f"no proposal {proposal_id!r}")
    target = slug or prop.slug_suggestion
    if await session.get(CapabilityModel, target) is None:
        session.add(CapabilityModel(
            slug=target, name=prop.name, description=prop.description,
            example=prop.example,
        ))
        await session.flush()
    # (capability_slug, category_slug) is a composite PK -- re-running promote()
    # (retry, or a second proposal converging on the same target) must not
    # attempt a duplicate tag insert.
    if await session.get(CapabilityCategoryTagModel, (target, primary_category)) is None:
        session.add(CapabilityCategoryTagModel(
            capability_slug=target, category_slug=primary_category, is_primary=True))
    for extra in extra_categories:
        if await session.get(CapabilityCategoryTagModel, (target, extra)) is None:
            session.add(CapabilityCategoryTagModel(
                capability_slug=target, category_slug=extra, is_primary=False))
    prop.status = "PROMOTED"
    prop.promoted_capability_slug = target
    prop.reviewed_at = utcnow()
    prop.reviewed_by = reviewed_by
    await session.flush()
    return target


async def reject(
    session, proposal_id: str, *, merge_into: str | None = None,
    reviewed_by: str = "cli",
) -> None:
    prop = await session.get(CapabilityProposalModel, proposal_id)
    if prop is None:
        raise ValueError(f"no proposal {proposal_id!r}")
    prop.status = "REJECTED"
    # Doubles as the merge target: findings citing the rejected slug resolve to
    # the survivor. Without one they stay orphaned and roll up as unclassified.
    prop.promoted_capability_slug = merge_into
    prop.reviewed_at = utcnow()
    prop.reviewed_by = reviewed_by
    await session.flush()


async def _main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("promote")
    p.add_argument("proposal_id")
    p.add_argument("--category", required=True)
    p.add_argument("--also", nargs="*", default=[])
    p.add_argument("--slug")
    r = sub.add_parser("reject")
    r.add_argument("proposal_id")
    r.add_argument("--merge-into")
    args = ap.parse_args()

    async with get_session() as session:
        if args.cmd == "list":
            for x in await list_pending(session):
                print(f"{x.id}  {x.slug_suggestion:36s} {x.name}")
                print(f"      {x.description}")
                print(f"      categories={x.category_slugs} trials={x.trial_ids}")
        elif args.cmd == "promote":
            print("promoted ->", await promote(
                session, args.proposal_id, primary_category=args.category,
                extra_categories=tuple(args.also), slug=args.slug))
        else:
            await reject(session, args.proposal_id, merge_into=args.merge_into)
            print("rejected", args.proposal_id)


if __name__ == "__main__":
    asyncio.run(_main())

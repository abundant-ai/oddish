"""Materialize schema, seed the preview org, stamp alembic chains."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())  # backend/ — find flat-layout `models`

from sqlalchemy import select  # noqa: E402

import models  # noqa: E402
from oddish.db import init_db, get_session  # noqa: E402


async def setup() -> None:
    await init_db()

    # Seed an org row matching the developer's Clerk org so JIT user
    # provisioning has something to attach to. Set
    # PREVIEW_SEED_CLERK_ORG_ID as a GitHub Actions variable in your
    # fork — the value is your Clerk organization id (`org_…`).
    clerk_org_id = os.environ.get("PREVIEW_SEED_CLERK_ORG_ID")
    if not clerk_org_id:
        return
    async with get_session() as session:
        existing = await session.execute(
            select(models.OrganizationModel)
            .where(models.OrganizationModel.clerk_org_id == clerk_org_id)
        )
        if existing.scalar_one_or_none():
            return
        session.add(models.OrganizationModel(
            id=models.generate_id(),
            name="Preview",
            slug=f"preview-{clerk_org_id.lower()}",
            clerk_org_id=clerk_org_id,
            is_active=True,
        ))


asyncio.run(setup())

for project in (Path.cwd().parent / "oddish", Path.cwd()):
    subprocess.run(["alembic", "stamp", "head"], cwd=project, check=True)

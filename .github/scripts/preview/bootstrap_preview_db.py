"""Run alembic on a fresh Supabase preview branch and seed the org row."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())  # backend/ — find flat-layout `models`

from sqlalchemy import select  # noqa: E402

import models  # noqa: E402
from oddish.db import get_session  # noqa: E402


# Forks: change to your Clerk org id.
SEED_CLERK_ORG_ID = "org_39ufkEqie8rLlVhoK4YMm4IMx0L"


for project in (Path.cwd().parent / "oddish", Path.cwd()):
    subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)


async def seed() -> None:
    async with get_session() as session:
        existing = await session.execute(
            select(models.OrganizationModel)
            .where(models.OrganizationModel.clerk_org_id == SEED_CLERK_ORG_ID)
        )
        if existing.scalar_one_or_none():
            return
        session.add(models.OrganizationModel(
            id=models.generate_id(),
            name="Preview",
            slug=f"preview-{SEED_CLERK_ORG_ID.lower()}",
            clerk_org_id=SEED_CLERK_ORG_ID,
            is_active=True,
        ))


asyncio.run(seed())

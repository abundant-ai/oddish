"""Materialize schema, seed the preview org, stamp alembic chains."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())  # backend/ — find flat-layout `models`

from sqlalchemy import select, text  # noqa: E402

import models  # noqa: E402
from oddish.db import init_db, get_session  # noqa: E402
from oddish.db.connection import engine  # noqa: E402


# Clerk org to seed on every fresh preview branch so JIT user
# provisioning has something to attach to. Forks: change this to your
# own org id.
SEED_CLERK_ORG_ID = "org_39ufkEqie8rLlVhoK4YMm4IMx0L"


async def setup() -> None:
    # Supabase branches inherit the parent project's `public` schema,
    # which may have drifted from the current model (e.g., an `id`
    # column on `queue_slots` left over from an earlier revision).
    # `init_db()` uses CREATE TABLE IF NOT EXISTS, so it would skip
    # those tables and we'd insert into a stale schema. Wipe `public`
    # first — branch data is throwaway.
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    await init_db()
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


asyncio.run(setup())

for project in (Path.cwd().parent / "oddish", Path.cwd()):
    subprocess.run(["alembic", "stamp", "head"], cwd=project, check=True)

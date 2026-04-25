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


async def _is_initialized() -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'alembic_version_oddish'"
        ))
        return result.first() is not None


async def setup() -> None:
    # First deploy on a fresh branch (or first deploy after a manual
    # reset): wipe `public` (Supabase branches inherit the parent's
    # drifted schema), then `create_all` from the current models and
    # `alembic stamp head` so subsequent runs can apply incremental
    # migrations on top.
    if not await _is_initialized():
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await init_db()
        for project in (Path.cwd().parent / "oddish", Path.cwd()):
            subprocess.run(["alembic", "stamp", "head"], cwd=project, check=True)
    else:
        # Subsequent deploys on the same branch: apply any new
        # migrations the PR has added since the last bootstrap.
        for project in (Path.cwd().parent / "oddish", Path.cwd()):
            subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)

    # Idempotent — only inserts if the org isn't already there.
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

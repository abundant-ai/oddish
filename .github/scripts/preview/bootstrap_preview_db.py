"""Apply Alembic to the PR's Supabase preview branch and seed the org row.

With Supabase data-branching enabled (``supabase/config.toml`` →
``[experimental.branching] with_data = true``), the preview branch is a
clone of prod: ``alembic_version`` already points at prod's head, every
table already has prod-shaped rows. ``alembic upgrade head`` then only
applies the revisions added on this PR — and those revisions run against
real data, so a destructive migration fails *here* instead of at the
prod cutover. That's the entire point of this step.

The seed below is idempotent so it works against either a freshly-cloned
branch (org row already present) or the legacy schema-only branch
(empty DB).
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())  # backend/ — find flat-layout `models`

from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

import models  # noqa: E402
from oddish.db import get_session  # noqa: E402


# Forks: change to your Clerk org id.
SEED_CLERK_ORG_ID = "org_39ufkEqie8rLlVhoK4YMm4IMx0L"


for project in (Path.cwd().parent / "oddish", Path.cwd()):
    subprocess.run(["alembic", "upgrade", "head"], cwd=project, check=True)


async def seed() -> None:
    """Insert the preview org row, idempotently.

    Cloned-data branches already have the prod org row. Pre-data-branching
    branches don't. ``ON CONFLICT (clerk_org_id) DO NOTHING`` collapses
    both into a single statement and lets reruns be no-ops.
    """
    async with get_session() as session:
        stmt = (
            pg_insert(models.OrganizationModel)
            .values(
                id=models.generate_id(),
                name="Preview",
                slug=f"preview-{SEED_CLERK_ORG_ID.lower()}",
                clerk_org_id=SEED_CLERK_ORG_ID,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["clerk_org_id"])
        )
        result = await session.execute(stmt)
        await session.commit()
        if result.rowcount:
            print(f"seeded organization clerk_org_id={SEED_CLERK_ORG_ID}")
        else:
            print(
                f"organization clerk_org_id={SEED_CLERK_ORG_ID} already present"
            )


asyncio.run(seed())

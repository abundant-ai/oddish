"""Idempotent DB seed for the authenticated dashboard Playwright e2e.

Run from ``backend/`` as ``uv run python tests/e2e/seed_dashboard.py``.

The dashboard test signs in as a Clerk dev-instance user whose JWT carries an
``org_id`` claim. The backend resolves that org via ``clerk_org_id`` lookup
(``backend/auth/provisioning.py::get_org_from_clerk_id``) and scopes every
``/tasks`` query to ``org.id`` — so we seed an OrganizationModel whose ``id``
*and* ``clerk_org_id`` both equal ``E2E_CLERK_ORG_ID``, plus one task owned by
that org so the detail page has something to render.

Schema note: the vanilla backend does not run ``create_all`` at startup (it
expects Alembic in hosted envs; ``backend/api/app.py`` lifespan skips DDL), and
the workflow boots ``serve.py`` directly with no conftest fixture — so this
script also builds the schema, mirroring the e2e conftest ``schema`` fixture.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine.url import make_url

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Deterministic ids so reruns land on the same rows (idempotency).
TASK_ID = "task_e2e_dashboard"
TASK_NAME = "task_e2e_dashboard"

# Mirrors the conftest `schema` fixture: create_all does not build the sweep's
# partial unique index, so replicate the DDL after it.
_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_jobs_tag_project_active "
    "ON worker_jobs (kind, subject_table, subject_id) "
    "WHERE kind = 'TAG_PROJECT' "
    "AND status IN ('QUEUED', 'RETRYING') "
    "AND subject_id IS NOT NULL"
)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"seed_dashboard: {name} is required but unset")
    return value


def _require_local_db(db_url: str) -> None:
    host = make_url(db_url).host
    if host not in _LOCAL_HOSTS:
        raise SystemExit(
            f"seed_dashboard refuses non-local database host {host!r}; "
            "point ODDISH_DATABASE_URL at a throwaway localhost container"
        )


async def _ensure_schema(db_url: str) -> None:
    import models  # noqa: F401  (registers cloud tables on the shared Base)
    from sqlalchemy.ext.asyncio import create_async_engine

    from oddish.db.models import Base

    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
            await conn.execute(text(_INDEX_DDL))
    finally:
        await engine.dispose()


async def _seed(org_id: str) -> str:
    from models import OrganizationModel
    from oddish.db import get_session

    async with get_session() as session:
        # Match on either column: clerk_org_id is unique, so an org that was
        # auto-provisioned with a generated primary id but the same Clerk org
        # must be reused, not re-inserted (which would hit the constraint).
        existing = await session.execute(
            select(OrganizationModel).where(
                (OrganizationModel.id == org_id)
                | (OrganizationModel.clerk_org_id == org_id)
            )
        )
        org = existing.scalars().first()
        if org is None:
            org = OrganizationModel(
                id=org_id,
                name=org_id,
                slug=org_id,
                clerk_org_id=org_id,
            )
            session.add(org)
        else:
            # A stale row (seeded before clerk_org_id mattered, or by an older
            # script version) would break JWT org resolution — repair it.
            org.clerk_org_id = org_id
        await session.flush()
        owner_org_id = org.id

        # Same column set as the conftest `seeded` fixture's raw task INSERT.
        # The upsert rebinds org_id and clears deleted_at so a stale row from a
        # previous E2E_CLERK_ORG_ID (or a soft-deleted one) is repaired, not
        # silently left pointing at the wrong org.
        await session.execute(
            text(
                "insert into tasks "
                '(id,name,org_id,"user",priority,status,task_path,tags,'
                "effective_tag_ids,current_version_tag_ids,run_analysis,run_probe,"
                "created_at,updated_at) "
                "values (:id,:name,:org,'u','LOW','COMPLETED','p','{}'::jsonb,"
                "'{}'::text[],'{}'::text[],false,false,now(),now()) "
                "on conflict (id) do update set "
                "org_id = excluded.org_id, deleted_at = null, updated_at = now()"
            ),
            {"id": TASK_ID, "name": TASK_NAME, "org": owner_org_id},
        )

    return TASK_ID


def main() -> None:
    db_url = _require("ODDISH_DATABASE_URL")
    org_id = _require("E2E_CLERK_ORG_ID")
    _require_local_db(db_url)

    asyncio.run(_ensure_schema(db_url))
    task_id = asyncio.run(_seed(org_id))

    print(f"task_id={task_id}")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"task_id={task_id}\n")


if __name__ == "__main__":
    main()

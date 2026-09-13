"""Copy operator approval decisions from production, separately from sample data."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


# Successful-access fixtures, not an approval list. Production must approve them.
REQUIRED_CLERK_ORG_IDS = (
    "org_39ufkEqie8rLlVhoK4YMm4IMx0L",  # Abundant
    "org_3G0Tg8XCxO5VRO63ssJh7SMyhTp",  # SRE-World
    "org_3H67wVrUZObfjW9JnxGq5pUZQvN",  # Abundant CyberMasters
)


async def read_approval_decisions(source: AsyncEngine) -> list[dict]:
    async with source.begin() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(text("SET LOCAL statement_timeout = '10000'"))
        has_column = await conn.scalar(
            text(
                "SELECT EXISTS (SELECT FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='organizations' "
                "AND column_name='execution_enabled')"
            )
        )
        if not has_column:
            raise RuntimeError(
                "Production organization approvals are not initialized; "
                "prepare production approval decisions before deploying previews."
            )
        rows = await conn.execute(
            text(
                "SELECT id, name, slug, clerk_org_id, "
                "(execution_enabled AND is_active AND deleted_at IS NULL) AS approved "
                "FROM organizations"
            )
        )
        return [dict(row) for row in rows.mappings()]


async def sync_approval_decisions(
    target: AsyncEngine,
    decisions: list[dict],
    *,
    required_clerk_ids: tuple[str, ...] = REQUIRED_CLERK_ORG_IDS,
) -> dict[str, int]:
    approved = [row for row in decisions if row["approved"]]
    missing = set(required_clerk_ids) - {row["clerk_org_id"] for row in approved}
    if missing:
        raise RuntimeError(
            "Required preview organizations are not approved in production: "
            + ", ".join(sorted(missing))
        )
    payload = {"decisions": json.dumps(decisions)}
    async with target.begin() as conn:
        await conn.execute(text("SET LOCAL statement_timeout = '10000'"))
        await conn.execute(text("SET LOCAL lock_timeout = '2000'"))
        # Include all approved identities, even when their tasks were not sampled.
        # Existing local IDs, deactivation, budgets, users and keys are preserved.
        inserted = await conn.execute(
            text("""
            INSERT INTO organizations
                (id, name, slug, clerk_org_id, plan, settings, is_active,
                 execution_enabled, created_at, updated_at)
            SELECT s.id, s.name, s.slug, s.clerk_org_id, 'free', '{}'::jsonb, true,
                   false, now(), now()
            FROM jsonb_to_recordset(CAST(:decisions AS jsonb))
                AS s(id text, name text, slug text, clerk_org_id text, approved boolean)
            WHERE approved AND NOT EXISTS (
                SELECT FROM organizations o
                WHERE o.clerk_org_id = s.clerk_org_id
                   OR (s.clerk_org_id IS NULL AND o.clerk_org_id IS NULL AND o.id = s.id)
            )
            ON CONFLICT DO NOTHING
            RETURNING id
        """),
            payload,
        )
        inserted_count = len(inserted.all())
        updated = await conn.execute(
            text("""
            WITH decisions AS (
                SELECT * FROM jsonb_to_recordset(CAST(:decisions AS jsonb))
                    AS s(id text, clerk_org_id text, approved boolean)
            ), desired AS (
                SELECT o.id, (COALESCE(s.approved, false) AND o.is_active
                             AND o.deleted_at IS NULL) AS approved
                FROM organizations o LEFT JOIN decisions s
                  ON o.clerk_org_id = s.clerk_org_id
                  OR (s.clerk_org_id IS NULL AND o.clerk_org_id IS NULL AND o.id = s.id)
            )
            UPDATE organizations o
            SET execution_enabled = d.approved, updated_at = now()
            FROM desired d
            WHERE o.id = d.id AND o.execution_enabled IS DISTINCT FROM d.approved
            RETURNING o.id
        """),
            payload,
        )
        updated_count = len(updated.all())
        inaccessible = await conn.execute(
            text("""
            SELECT COALESCE(s.clerk_org_id, s.id)
            FROM jsonb_to_recordset(CAST(:decisions AS jsonb))
                AS s(id text, clerk_org_id text, approved boolean)
            LEFT JOIN organizations o
              ON o.clerk_org_id = s.clerk_org_id
              OR (s.clerk_org_id IS NULL AND o.clerk_org_id IS NULL AND o.id = s.id)
            WHERE s.approved AND (o.id IS NULL OR NOT o.execution_enabled)
        """),
            payload,
        )
        missing = list(inaccessible.scalars())
        if missing:
            raise RuntimeError(
                "Production-approved organizations are inaccessible in preview: "
                + ", ".join(sorted(missing))
            )
        return {
            "approved": len(approved),
            "inserted": inserted_count,
            "updated": updated_count,
        }

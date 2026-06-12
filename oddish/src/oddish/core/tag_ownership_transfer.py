"""Auto-reassign ``tags.owner_user_id`` when an owner is deactivated.

Called from two sites so a deactivation never strands a tag:
1. ``backend/api/routers/orgs.py`` (admin removes a member) — fires
   transactionally in the same session as the user soft-delete.
2. ``oddish/src/oddish/workers/queue/cleanup.py`` hourly sweep — catches
   any tag rows whose owner was deactivated by an external path.

The new owner is the org's oldest ACTIVE OWNER user; if none exists the
tag's owner becomes NULL (org-owned, admin-managed).
"""
from __future__ import annotations

from sqlalchemy import text


async def _resolve_org_admin_user_id(session, *, org_id: str) -> str | None:
    return await session.scalar(
        text(
            """
            SELECT id
            FROM users
            WHERE org_id = :org_id
              AND deleted_at IS NULL
              AND is_active = TRUE
              AND role = 'OWNER'
            ORDER BY created_at
            LIMIT 1
            """
        ),
        {"org_id": org_id},
    )


async def transfer_tag_ownership_to_admin(
    session, *, org_id: str, deactivated_user_id: str
) -> int:
    """Reassign every tag owned by ``deactivated_user_id`` in ``org_id``
    to the oldest active org admin (or NULL if there is none). Returns
    the number of tag rows updated.
    """
    admin_id = await _resolve_org_admin_user_id(session, org_id=org_id)
    result = await session.execute(
        text(
            """
            UPDATE tags
            SET owner_user_id = :admin_id,
                updated_at    = NOW()
            WHERE org_id = :org_id
              AND deleted_at IS NULL
              AND state <> 'DELETED'
              AND owner_user_id = :user_id
            """
        ),
        {
            "admin_id": admin_id,
            "org_id": org_id,
            "user_id": deactivated_user_id,
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def sweep_orphaned_tag_owners(session) -> int:
    """Sweep all tags whose ``owner_user_id`` no longer points at a live
    user. For each affected org, recompute the admin once and update the
    rows in a single statement.
    """
    affected_orgs = (
        await session.execute(
            text(
                """
                SELECT DISTINCT t.org_id
                FROM tags t
                LEFT JOIN users u
                  ON u.id = t.owner_user_id
                 AND u.deleted_at IS NULL
                 AND u.is_active = TRUE
                WHERE t.deleted_at IS NULL
                  AND t.state <> 'DELETED'
                  AND t.owner_user_id IS NOT NULL
                  AND u.id IS NULL
                """
            )
        )
    ).all()
    total = 0
    for (org_id,) in affected_orgs:
        if not org_id:
            continue
        admin_id = await _resolve_org_admin_user_id(session, org_id=str(org_id))
        result = await session.execute(
            text(
                """
                UPDATE tags t
                SET owner_user_id = :admin_id,
                    updated_at    = NOW()
                WHERE t.deleted_at IS NULL
                  AND t.state <> 'DELETED'
                  AND t.org_id = :org_id
                  AND t.owner_user_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM users u
                      WHERE u.id = t.owner_user_id
                        AND u.deleted_at IS NULL
                        AND u.is_active = TRUE
                  )
                """
            ),
            {"admin_id": admin_id, "org_id": str(org_id)},
        )
        total += int(getattr(result, "rowcount", 0) or 0)
    return total

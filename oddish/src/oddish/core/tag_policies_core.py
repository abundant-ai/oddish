"""Per-org governance policy CRUD + the 10-tag cap helper."""
from __future__ import annotations

from sqlalchemy import text


class TagCapExceededError(ValueError):
    pass


async def get_or_create_tag_policy(session, *, org_id: str) -> dict:
    """Return the policy row for ``org_id``; insert defaults if absent."""
    await session.execute(
        text(
            """
            INSERT INTO tag_policies (org_id, updated_at)
            VALUES (:org_id, NOW())
            ON CONFLICT (org_id) DO NOTHING
            """
        ),
        {"org_id": org_id},
    )
    row = (
        await session.execute(
            text("SELECT * FROM tag_policies WHERE org_id = :org_id"),
            {"org_id": org_id},
        )
    ).all()
    if not row:
        return {
            "org_id": org_id,
            "max_tags_per_entity": 10,
            "name_max_len": 64,
            "name_charset": "[a-z0-9._-]",
            "reserved_prefixes": [],
            "who_can_create": "ANY_MEMBER",
            "profanity_mode": "ENFORCE",
            "profanity_allowlist": [],
            "profanity_denylist": [],
        }
    first = row[0]
    return dict(getattr(first, "_mapping", first))


async def update_tag_policy_core(
    session,
    *,
    org_id: str,
    updates: dict,
    actor_user_id: str | None,
) -> None:
    """Upsert per-field updates into ``tag_policies``.

    The list-typed columns are Postgres ``TEXT[]`` — asyncpg binds a Python
    list directly to a TEXT[] parameter, so we pass every value as a plain
    bind param (no JSONB cast).
    """
    columns_allowed = {
        "max_tags_per_entity",
        "name_max_len",
        "name_charset",
        "reserved_prefixes",
        "who_can_create",
        "profanity_mode",
        "profanity_allowlist",
        "profanity_denylist",
    }
    set_pairs: list[str] = []
    params: dict[str, object] = {"org_id": org_id, "updated_by_user_id": actor_user_id}
    for key, val in updates.items():
        if key not in columns_allowed:
            raise ValueError(f"unknown tag_policy column: {key}")
        params[key] = val
        set_pairs.append(f"{key} = :{key}")
    set_pairs.append("updated_by_user_id = :updated_by_user_id")
    set_pairs.append("updated_at = NOW()")
    await session.execute(
        text(
            "INSERT INTO tag_policies (org_id, updated_at) VALUES (:org_id, NOW()) "
            "ON CONFLICT (org_id) DO UPDATE SET " + ", ".join(set_pairs)
        ),
        params,
    )


async def count_direct_active_tags(
    session,
    *,
    scope: str,
    target_id: str,
    org_id: str | None,
) -> int:
    """Count ACTIVE direct (DIRECT-source) tag assignments at the target's
    own scope. Inherited assignments are intentionally excluded from the
    cap."""
    n = await session.scalar(
        text(
            """
            SELECT COUNT(*)
            FROM tag_assignments
            WHERE deleted_at IS NULL
              AND state = 'ACTIVE'
              AND source = 'DIRECT'
              AND scope = :scope
              AND target_id = :target_id
              AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
            """
        ),
        {"scope": scope, "target_id": target_id, "org_id": org_id},
    )
    return int(n or 0)


async def assert_under_tag_cap(
    session,
    *,
    scope: str,
    target_id: str,
    org_id: str | None,
    max_tags_per_entity: int,
) -> None:
    n = await count_direct_active_tags(
        session, scope=scope, target_id=target_id, org_id=org_id
    )
    if n >= max_tags_per_entity:
        raise TagCapExceededError(
            f"target {scope}:{target_id} already has {n} direct tags "
            f"(cap={max_tags_per_entity})"
        )

"""Saved tag-filter CRUD.

Saved filters persist stable tag IDs (so renames don't break them).
``merged_into_id`` is followed at read so a merged source tag resolves to
the survivor.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text


async def list_saved_tag_filters_core(
    session, *, org_id: str, actor_user_id: str
) -> list[dict]:
    rows = (
        await session.execute(
            text(
                """
                SELECT id, name, filter_ast, visibility, owner_user_id
                FROM saved_tag_filters
                WHERE deleted_at IS NULL
                  AND org_id = :org_id
                  AND (
                      owner_user_id = :actor_user_id
                   OR visibility = 'ORG'
                  )
                ORDER BY updated_at DESC, id ASC
                """
            ),
            {"org_id": org_id, "actor_user_id": actor_user_id},
        )
    ).all()
    out: list[dict] = []
    for row in rows:
        id_, name, ast_raw, visibility, owner = row[0], row[1], row[2], row[3], row[4]
        out.append(
            {
                "id": str(id_),
                "name": str(name),
                "filter_ast": (
                    ast_raw if isinstance(ast_raw, dict) else json.loads(ast_raw or "{}")
                ),
                "visibility": str(visibility),
                "owner_user_id": str(owner),
            }
        )
    return out


async def create_saved_tag_filter_core(
    session,
    *,
    org_id: str,
    owner_user_id: str,
    name: str,
    filter_ast: dict,
    visibility: str = "PRIVATE",
) -> str:
    new_id = str(uuid.uuid4())[:16]
    await session.execute(
        text(
            """
            INSERT INTO saved_tag_filters
                (id, org_id, owner_user_id, name, filter_ast, visibility,
                 created_at, updated_at)
            VALUES
                (:id, :org_id, :owner, :name, CAST(:ast AS JSONB),
                 :visibility, NOW(), NOW())
            """
        ),
        {
            "id": new_id,
            "org_id": org_id,
            "owner": owner_user_id,
            "name": name,
            "ast": json.dumps(filter_ast),
            "visibility": visibility,
        },
    )
    return new_id


async def update_saved_tag_filter_core(
    session,
    *,
    filter_id: str,
    actor_user_id: str,
    org_id: str | None = None,
    updates: dict,
) -> None:
    columns = {"name", "filter_ast", "visibility"}
    set_pairs: list[str] = []
    params: dict[str, object] = {
        "org_id": org_id,
        "filter_id": filter_id,
        "actor_user_id": actor_user_id,
    }
    for k, v in updates.items():
        if k not in columns:
            raise ValueError(f"unknown saved_tag_filter column: {k}")
        if k == "filter_ast":
            params[k] = json.dumps(v)
            set_pairs.append(f"{k} = CAST(:{k} AS JSONB)")
        else:
            params[k] = v
            set_pairs.append(f"{k} = :{k}")
    if not set_pairs:
        return
    set_pairs.append("updated_at = NOW()")
    await session.execute(
        text(
            f"""
            UPDATE saved_tag_filters
            SET {", ".join(set_pairs)}
            WHERE id = :filter_id
              AND owner_user_id = :actor_user_id
              AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
              AND deleted_at IS NULL
            """
        ),
        params,
    )


async def delete_saved_tag_filter_core(
    session, *, filter_id: str, actor_user_id: str, org_id: str | None = None
) -> None:
    await session.execute(
        text(
            """
            UPDATE saved_tag_filters
            SET deleted_at = NOW(),
                updated_at = NOW()
            WHERE id = :filter_id
              AND owner_user_id = :actor_user_id
              AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
              AND deleted_at IS NULL
            """
        ),
        {"filter_id": filter_id, "actor_user_id": actor_user_id, "org_id": org_id},
    )

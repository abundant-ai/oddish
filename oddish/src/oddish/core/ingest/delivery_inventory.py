"""Export a read-only, organization-scoped task inventory for delivery backfill.

Served by ``GET /deliveries/task-inventory``; the route lends the read
session. Includes retired tasks so their IDs remain available for history.
Nothing here writes rows, exports credentials, or enqueues QA.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

INVENTORY_SCHEMA = "oddish-task-inventory-v1"
# Standalone-server rows carry ``org_id`` NULL; documents need a string label.
LOCAL_ORG_LABEL = "local"


def required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def org_label(org_id: str | None) -> str:
    return org_id if org_id else LOCAL_ORG_LABEL


def org_clause(column, org_id: str | None):
    return column.is_(None) if org_id is None else column == org_id


def inventory_task(
    row: dict, tag_definitions: dict[str, dict], *, label: str | None = None
) -> dict:
    categories = []
    tags = row["tags"] or {}
    github = tags.get("github_meta") or {}
    if isinstance(github, str):
        try:
            github = json.loads(github)
        except ValueError:
            github = {}
    if not isinstance(github, dict):
        github = {}
    for location, value in [
        ("tags.category", tags.get("category")),
        ("tags.github_meta.category", github.get("category")),
    ]:
        if isinstance(value, str) and value.strip():
            categories.append({"source": location, "value": value.strip()})
    for tag_id in row["current_version_tag_ids"] or []:
        tag = tag_definitions.get(tag_id)
        if tag and tag.get("merged_into_id"):
            tag = tag_definitions.get(tag["merged_into_id"])
        if tag and tag["normalized_key"] == "category" and tag.get("value"):
            categories.append({"source": f"tag:{tag['id']}", "value": tag["value"]})
    return {
        "id": row["id"],
        "name": row["name"],
        "org_id": label or row["org_id"],
        "task_path": row["task_path"],
        "retired_at": row["deleted_at"].isoformat() if row["deleted_at"] else None,
        "current_version_id": row["current_version_id"],
        "current_content_hash": row["content_hash"],
        "categories": sorted({c["value"] for c in categories}),
        "category_evidence": sorted(
            categories, key=lambda c: (c["source"], c["value"])
        ),
    }


async def export_inventory_core(session, *, org_id: str | None) -> dict:
    """Current task identities for one organization, retired tasks included.

    Exports current IDs and hashes as current evidence only. Categories come
    from legacy task tags, GitHub metadata, and structured category tags;
    differing values are retained.
    """
    from sqlalchemy import select
    from oddish.db.models import TagModel, TaskModel, TaskVersionModel

    label = org_label(org_id)
    tags = (
        (
            await session.execute(
                select(
                    TagModel.id,
                    TagModel.normalized_key,
                    TagModel.value,
                    TagModel.merged_into_id,
                ).where(
                    org_clause(TagModel.org_id, org_id), TagModel.state != "DELETED"
                )
            )
        )
        .mappings()
        .all()
    )
    tag_definitions = {row["id"]: dict(row) for row in tags}
    tasks = []
    last_id = None
    while True:
        query = (
            select(
                TaskModel.id,
                TaskModel.name,
                TaskModel.org_id,
                TaskModel.task_path,
                TaskModel.deleted_at,
                TaskModel.tags,
                TaskModel.current_version_id,
                TaskModel.current_version_tag_ids,
                TaskVersionModel.content_hash,
            )
            .outerjoin(
                TaskVersionModel, TaskModel.current_version_id == TaskVersionModel.id
            )
            .where(org_clause(TaskModel.org_id, org_id))
            .order_by(TaskModel.id)
            .limit(500)
            .execution_options(include_deleted=True)
        )
        if last_id is not None:
            query = query.where(TaskModel.id > last_id)
        rows = (await session.execute(query)).mappings().all()
        if not rows:
            break
        tasks.extend(
            inventory_task(dict(row), tag_definitions, label=label) for row in rows
        )
        last_id = rows[-1]["id"]
    return {
        "schema_version": INVENTORY_SCHEMA,
        "org_id": label,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "tasks": tasks,
    }

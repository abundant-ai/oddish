"""Derive tag assignments from task.toml metadata.

Assignments use TagAssignmentSource.DERIVED at TASK scope and are owned
entirely by this module: each rebuild replaces the full set.

TASK scope is mandatory, not stylistic. tasks.effective_tag_ids is the UNION
across every version (core/tags/projection.py:125-138), and that is the column
browse filtering reads (core/tags/filter_ast.py:144). At VERSION scope, a
rebuild could not retract an older version's assignment, so a task
recategorized from ml-training to optimization would match both categories
forever with no way to correct it.

Retraction is source-filtered raw SQL, not ``unassign_tag_core``, because the
target-uniqueness index has no ``source`` column: a naive retraction could
flip -- or delete -- a human's DIRECT assignment of the same tag.
"""

from __future__ import annotations

from sqlalchemy import text

from oddish.core.tags.enqueue import enqueue_tag_project_worker_job
from oddish.core.tags.policies import get_or_create_tag_policy
from oddish.core.tags.projection import recompute_task_browse_projection
from oddish.core.tags.service import assign_tag_core, create_tag_core
from oddish.core.task_metadata import slugify
from oddish.schemas import TaskMetadata


def derived_tag_pairs(metadata: TaskMetadata) -> list[tuple[str, str]]:
    """Map metadata onto (tag_key, tag_value) pairs, in stable order."""
    pairs: list[tuple[str, str]] = []
    if metadata.category:
        pairs.append(("category", metadata.category))
    for topic in metadata.topic_tags:
        slug = slugify(topic)
        if slug:
            pairs.append(("topic", slug))
    if metadata.author_organization:
        slug = slugify(metadata.author_organization)
        if slug:
            pairs.append(("org", slug))
    return pairs


# tag_policies.org_id is a NOT NULL primary key, so org-less tasks (local /
# self-hosted uploads) have no governance row to fetch -- fall back to the
# same defaults get_or_create_tag_policy uses when a row is absent.
_NO_ORG_POLICY = {
    "max_tags_per_entity": 10,
    "name_max_len": 64,
    "name_charset": "[a-z0-9._-]",
    "reserved_prefixes": [],
    "who_can_create": "ANY_MEMBER",
    "profanity_mode": "ENFORCE",
    "profanity_allowlist": [],
    "profanity_denylist": [],
}

_EXISTING_NON_DERIVED = text(
    """
    SELECT tag_id FROM tag_assignments
    WHERE deleted_at IS NULL
      AND state = 'ACTIVE'
      AND source <> 'DERIVED'
      AND scope = 'TASK'
      AND target_id = :task_id
      AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
    """
)

_RETRACT_STALE_DERIVED = text(
    """
    UPDATE tag_assignments
    SET state = 'REMOVED',
        row_version = row_version + 1,
        removed_at = NOW(),
        updated_at = NOW()
    WHERE deleted_at IS NULL
      AND state = 'ACTIVE'
      AND source = 'DERIVED'
      AND scope = 'TASK'
      AND target_id = :task_id
      AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
      AND (
          array_length(CAST(:keep_ids AS TEXT[]), 1) IS NULL
          OR NOT (tag_id = ANY(CAST(:keep_ids AS TEXT[])))
      )
    """
)


async def rebuild_derived_tags(
    session, *, task_id: str, org_id: str | None, metadata: TaskMetadata
) -> None:
    """Replace this task's DERIVED tags with the set implied by *metadata*.

    Resolve desired pairs to tag ids first, retract every stale ACTIVE
    DERIVED assignment not in that set, then upsert the desired ones --
    retract-then-assign so a tag that stays in the set is never briefly
    absent. A desired pair is skipped entirely when the tag already has an
    ACTIVE non-DERIVED (human) assignment on this task: DIRECT wins.
    """
    pairs = derived_tag_pairs(metadata)
    policy = (
        await get_or_create_tag_policy(session, org_id=org_id)
        if org_id is not None
        else _NO_ORG_POLICY
    )

    tag_ids: list[str] = []
    for key, value in pairs:
        tag_id = await create_tag_core(
            session,
            key=key,
            value=value,
            org_id=org_id,
            actor_user_id=None,
            policy=policy,
            is_admin=True,
        )
        tag_ids.append(tag_id)

    existing_non_derived = {
        str(row[0])
        for row in (
            await session.execute(
                _EXISTING_NON_DERIVED, {"task_id": task_id, "org_id": org_id}
            )
        ).all()
    }

    await session.execute(
        _RETRACT_STALE_DERIVED,
        {"task_id": task_id, "org_id": org_id, "keep_ids": tag_ids},
    )

    for tag_id in tag_ids:
        if tag_id in existing_non_derived:
            continue
        await assign_tag_core(
            session,
            tag_id=tag_id,
            scope="TASK",
            target_id=task_id,
            task_id=task_id,
            org_id=org_id,
            actor_user_id=None,
            source="DERIVED",
        )

    await recompute_task_browse_projection(session, task_id=task_id)
    await enqueue_tag_project_worker_job(
        session,
        scope="TASK",
        target_id=task_id,
        task_id=task_id,
        org_id=org_id,
        mode="task_all_versions",
    )

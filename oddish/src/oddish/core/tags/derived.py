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
from oddish.core.tags.naming import TagNameError
from oddish.core.tags.policies import get_or_create_tag_policy
from oddish.core.tags.projection import recompute_task_browse_projection
from oddish.core.tags.service import (
    TagPolicyError,
    TagProfanityError,
    assign_tag_core,
    create_tag_core,
)
from oddish.core.task_metadata import slugify
from oddish.schemas import TaskMetadata

# Metadata is best-effort: it must never fail an upload. Callers wrap the
# rebuild_derived_tags call in try/except DERIVED_TAG_ERRORS rather than
# letting a malformed vocabulary value (bad charset, profanity) 500 the
# whole request.
DERIVED_TAG_ERRORS = (TagNameError, TagPolicyError, TagProfanityError)

_DEFAULT_TAG_NAME_MAX_LEN = 64


def derived_tag_pairs(
    metadata: TaskMetadata, *, max_len: int = _DEFAULT_TAG_NAME_MAX_LEN
) -> list[tuple[str, str]]:
    """Map metadata onto (tag_key, tag_value) pairs, in stable order.

    Values are slugified AND truncated to the tag policy's ``max_len`` here
    (not just charset-normalized) because TaskMetadata's Pydantic bounds
    (128 chars) are wider than the default tag policy's name_max_len (64):
    an ordinary long value must be clamped to fit, not rejected downstream.
    """
    pairs: list[tuple[str, str]] = []
    if metadata.category:
        slug = slugify(metadata.category, max_len=max_len)
        if slug:
            pairs.append(("category", slug))
    for topic in metadata.topic_tags:
        slug = slugify(topic, max_len=max_len)
        if slug:
            pairs.append(("topic", slug))
    if metadata.author_organization:
        slug = slugify(metadata.author_organization, max_len=max_len)
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


# No state = 'ACTIVE' filter: a REMOVED DIRECT row is still human-owned. If
# excluded here, assign_tag_core's upsert CASE would resurrect it as ACTIVE
# with source='DIRECT' on the next rebuild, and _RETRACT_STALE_DERIVED (which
# only touches source='DERIVED') could never retract it again -- the DIRECT
# assignment would be stuck ACTIVE forever.
_EXISTING_NON_DERIVED = text(
    """
    SELECT tag_id FROM tag_assignments
    WHERE deleted_at IS NULL
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
    policy = (
        await get_or_create_tag_policy(session, org_id=org_id)
        if org_id is not None
        else _NO_ORG_POLICY
    )
    pairs = derived_tag_pairs(
        metadata, max_len=int(policy.get("name_max_len", _DEFAULT_TAG_NAME_MAX_LEN))
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
            sync_projection=False,
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

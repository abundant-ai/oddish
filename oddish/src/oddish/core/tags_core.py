"""Write-side core for tag CRUD, assignment, merge, exclusion, grants.

Every state-changing helper:
  * uses optimistic concurrency (row_version) where applicable,
  * emits a ``TagEventModel`` row in the same transaction,
  * enqueues a sibling ``worker_jobs(kind=TAG_PROJECT)`` row so the
    projection arrays stay consistent.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from oddish.core.tag_naming import (
    TagNameError,
    is_reserved_prefix,
    normalize_tag_key,
    normalize_tag_value,
    validate_tag_key,
    validate_tag_value,
)
from oddish.core.tag_profanity import check_tag_text
from oddish.core.tags_enqueue import enqueue_tag_project_worker_job
from oddish.core.tags_projection import recompute_task_browse_projection


def _new_event_uuid() -> str:
    return str(uuid.uuid4())


async def _emit_tag_event(
    session,
    *,
    action: str,
    org_id: str | None,
    tag_id: str | None,
    scope: str | None,
    target_id: str | None,
    actor_user_id: str | None,
    actor_type: str = "USER",
    source: str = "API",
    reason: str | None = None,
    payload: dict | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO tag_events
                (event_uuid, org_id, action, tag_id, scope, target_id,
                 actor_user_id, actor_type, source, reason, payload, occurred_at)
            VALUES
                (:event_uuid, :org_id, :action, :tag_id, :scope, :target_id,
                 :actor_user_id, :actor_type, :source, :reason,
                 CAST(:payload AS JSONB), NOW())
            """
        ),
        {
            "event_uuid": _new_event_uuid(),
            "org_id": org_id,
            "action": action,
            "tag_id": tag_id,
            "scope": scope,
            "target_id": target_id,
            "actor_user_id": actor_user_id,
            "actor_type": actor_type,
            "source": source,
            "reason": reason,
            "payload": json.dumps(payload or {}),
        },
    )


async def _resolve_projection_task_id(
    session, *, scope: str, target_id: str, task_id: str | None
) -> str | None:
    """Derive the owning task id when the caller omitted it.

    Projection invalidation (sync browse recompute + TAG_PROJECT enqueue) is
    keyed on ``task_id``; the API allows omitting it, and without this
    derivation a VERSION/TASK write would leave ``tasks.effective_tag_ids``
    stale until the hourly reconciler.
    """
    if task_id:
        return task_id
    if scope == "TASK":
        return target_id
    if scope == "VERSION":
        return await session.scalar(
            text("SELECT task_id FROM task_versions WHERE id = :version_id"),
            {"version_id": target_id},
        )
    return None


async def assign_tag_core(
    session,
    *,
    tag_id: str,
    scope: str,
    target_id: str,
    task_id: str | None,
    org_id: str | None,
    actor_user_id: str | None,
    source: str = "DIRECT",
    source_experiment_id: str | None = None,
    source_assignment_id: str | None = None,
) -> str:
    """Idempotent apply with resurrection.

    Returns the assignment id (existing if already present, else new).
    """
    task_id = await _resolve_projection_task_id(
        session, scope=scope, target_id=target_id, task_id=task_id
    )
    new_id = str(uuid.uuid4())[:16]
    rows = (
        await session.execute(
            text(
                """
                INSERT INTO tag_assignments
                    (id, tag_id, org_id, scope, target_id, task_id, state, source,
                     source_experiment_id, source_assignment_id, row_version,
                     assigned_by_user_id, assigned_at, created_at, updated_at)
                VALUES
                    (:id, :tag_id, :org_id, :scope, :target_id, :task_id,
                     'ACTIVE', :source, :source_experiment_id, :source_assignment_id,
                     1, :actor_user_id, NOW(), NOW(), NOW())
                ON CONFLICT (COALESCE(org_id, ''), tag_id, scope, target_id)
                    WHERE deleted_at IS NULL
                DO UPDATE SET state = 'ACTIVE',
                              row_version = tag_assignments.row_version + 1,
                              removed_at = NULL,
                              updated_at = NOW(),
                              assigned_by_user_id = EXCLUDED.assigned_by_user_id,
                              source = CASE
                                  WHEN EXCLUDED.source = 'DIRECT'
                                    OR tag_assignments.source = 'DIRECT'
                                  THEN 'DIRECT'::tag_assignment_source
                                  ELSE EXCLUDED.source
                              END,
                              source_experiment_id = CASE
                                  WHEN EXCLUDED.source = 'DIRECT'
                                    OR tag_assignments.source = 'DIRECT'
                                  THEN NULL
                                  ELSE EXCLUDED.source_experiment_id
                              END
                RETURNING id
                """
            ),
            {
                "id": new_id,
                "tag_id": tag_id,
                "org_id": org_id,
                "scope": scope,
                "target_id": target_id,
                "task_id": task_id,
                "source": source,
                "source_experiment_id": source_experiment_id,
                "source_assignment_id": source_assignment_id,
                "actor_user_id": actor_user_id,
            },
        )
    ).all()
    resolved_id = str(rows[0][0]) if rows else new_id
    await _emit_tag_event(
        session,
        action="APPLY",
        org_id=org_id,
        tag_id=tag_id,
        scope=scope,
        target_id=target_id,
        actor_user_id=actor_user_id,
        payload={"source": source},
    )

    # Sync-direct: directly-affected task's browse projection.
    if task_id:
        await recompute_task_browse_projection(session, task_id=task_id)

    # Async fan-out for the rest.
    if scope == "VERSION" and task_id:
        await enqueue_tag_project_worker_job(
            session,
            scope="VERSION",
            target_id=target_id,
            task_id=task_id,
            org_id=org_id,
            mode="direct",
        )
    elif scope == "TASK" and task_id:
        await enqueue_tag_project_worker_job(
            session,
            scope="TASK",
            target_id=task_id,
            task_id=task_id,
            org_id=org_id,
            mode="task_all_versions",
        )
    elif scope == "EXPERIMENT":
        await enqueue_tag_project_worker_job(
            session,
            scope="EXPERIMENT",
            target_id=target_id,
            task_id=None,
            org_id=org_id,
            mode="experiment_living_fanout",
        )

    return resolved_id


async def unassign_tag_core(
    session,
    *,
    tag_id: str,
    scope: str,
    target_id: str,
    task_id: str | None,
    org_id: str | None,
    actor_user_id: str | None,
) -> bool:
    """Soft-REMOVE the direct assignment; returns True iff a row flipped.

    Snapshot-experiment-derived assignments are touched too (we delete the
    materialized child via ``source_experiment_id`` when the experiment
    tag is later unassigned).
    """
    task_id = await _resolve_projection_task_id(
        session, scope=scope, target_id=target_id, task_id=task_id
    )
    result = await session.execute(
        text(
            """
            UPDATE tag_assignments
            SET state = 'REMOVED',
                row_version = row_version + 1,
                removed_at = NOW(),
                updated_at = NOW()
            WHERE deleted_at IS NULL
              AND state = 'ACTIVE'
              AND tag_id = :tag_id
              AND scope = :scope
              AND target_id = :target_id
              AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
            """
        ),
        {
            "tag_id": tag_id,
            "scope": scope,
            "target_id": target_id,
            "org_id": org_id,
        },
    )
    flipped = (getattr(result, "rowcount", 0) or 0) > 0
    await _emit_tag_event(
        session,
        action="REMOVE",
        org_id=org_id,
        tag_id=tag_id,
        scope=scope,
        target_id=target_id,
        actor_user_id=actor_user_id,
    )

    if task_id:
        await recompute_task_browse_projection(session, task_id=task_id)

    if scope == "VERSION" and task_id:
        await enqueue_tag_project_worker_job(
            session,
            scope="VERSION",
            target_id=target_id,
            task_id=task_id,
            org_id=org_id,
            mode="direct",
        )
    elif scope == "TASK" and task_id:
        await enqueue_tag_project_worker_job(
            session,
            scope="TASK",
            target_id=task_id,
            task_id=task_id,
            org_id=org_id,
            mode="task_all_versions",
        )
    elif scope == "EXPERIMENT":
        await enqueue_tag_project_worker_job(
            session,
            scope="EXPERIMENT",
            target_id=target_id,
            task_id=None,
            org_id=org_id,
            mode="experiment_living_fanout",
        )
    return flipped


async def _list_experiment_member_task_ids(session, experiment_id: str) -> list[str]:
    rows = (
        await session.execute(
            text(
                """
                SELECT task_id
                FROM task_experiments
                WHERE experiment_id = :exp_id
                  AND deleted_at IS NULL
                """
            ),
            {"exp_id": experiment_id},
        )
    ).all()
    return [str(r[0]) for r in rows]


async def apply_experiment_tag_core(
    session,
    *,
    tag_id: str,
    experiment_id: str,
    mode: str,
    org_id: str | None,
    actor_user_id: str | None,
) -> dict:
    """Apply a tag to an experiment.

    ``mode='snapshot'`` materializes one TASK-scope assignment per current
    member task, marked ``source='EXPERIMENT_SNAPSHOT'`` with
    ``source_experiment_id=experiment_id``. New members are not auto-tagged.

    ``mode='living'`` writes a single EXPERIMENT-scope row marked
    ``source='EXPERIMENT_LIVING'``; new and current members inherit it.
    """
    if mode == "snapshot":
        member_ids = await _list_experiment_member_task_ids(session, experiment_id)
        materialized = 0
        for task_id in member_ids:
            await assign_tag_core(
                session,
                tag_id=tag_id,
                scope="TASK",
                target_id=task_id,
                task_id=task_id,
                org_id=org_id,
                actor_user_id=actor_user_id,
                source="EXPERIMENT_SNAPSHOT",
                source_experiment_id=experiment_id,
            )
            materialized += 1
        return {"mode": "snapshot", "materialized": materialized}

    if mode == "living":
        await assign_tag_core(
            session,
            tag_id=tag_id,
            scope="EXPERIMENT",
            target_id=experiment_id,
            task_id=None,
            org_id=org_id,
            actor_user_id=actor_user_id,
            source="EXPERIMENT_LIVING",
            source_experiment_id=experiment_id,
        )
        return {"mode": "living"}

    raise ValueError(f"Unknown experiment-tag mode: {mode}")


async def remove_experiment_tag_core(
    session,
    *,
    tag_id: str,
    experiment_id: str,
    org_id: str | None,
    actor_user_id: str | None,
) -> dict:
    """Remove an experiment tag. Living: REMOVE the EXPERIMENT row.
    Snapshot: REMOVE every materialized TASK row that carries
    ``source_experiment_id=experiment_id``. Individually-removed snapshot
    rows stay REMOVED (no resurrection)."""
    await unassign_tag_core(
        session,
        tag_id=tag_id,
        scope="EXPERIMENT",
        target_id=experiment_id,
        task_id=None,
        org_id=org_id,
        actor_user_id=actor_user_id,
    )

    result = await session.execute(
        text(
            """
            UPDATE tag_assignments
            SET state = 'REMOVED',
                row_version = row_version + 1,
                removed_at = NOW(),
                updated_at = NOW()
            WHERE deleted_at IS NULL
              AND state = 'ACTIVE'
              AND tag_id = :tag_id
              AND scope = 'TASK'
              AND source_experiment_id = :exp_id
            RETURNING task_id
            """
        ),
        {"tag_id": tag_id, "exp_id": experiment_id},
    )
    affected_task_ids = [str(r[0]) for r in result.all() if r[0] is not None]
    for task_id in affected_task_ids:
        await _emit_tag_event(
            session,
            action="REMOVE",
            org_id=org_id,
            tag_id=tag_id,
            scope="TASK",
            target_id=task_id,
            actor_user_id=actor_user_id,
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
    return {"snapshot_removed": len(affected_task_ids)}


async def exclude_tag_core(
    session,
    *,
    tag_id: str,
    experiment_id: str,
    scope: str,
    target_id: str,
    org_id: str | None,
    actor_user_id: str | None,
    task_id: str | None = None,
) -> None:
    """Insert (or restore) a per-target tombstone that suppresses a
    living EXPERIMENT tag for a specific task or version."""
    new_id = str(uuid.uuid4())[:16]
    await session.execute(
        text(
            """
            INSERT INTO tag_exclusions
                (id, tag_id, org_id, experiment_id, scope, target_id,
                 created_by_user_id, created_at, updated_at)
            VALUES
                (:id, :tag_id, :org_id, :exp_id, :scope, :target_id,
                 :actor_user_id, NOW(), NOW())
            ON CONFLICT (experiment_id, tag_id, scope, target_id)
                WHERE deleted_at IS NULL
            DO UPDATE SET updated_at = NOW(),
                          deleted_at = NULL
            """
        ),
        {
            "id": new_id,
            "tag_id": tag_id,
            "org_id": org_id,
            "exp_id": experiment_id,
            "scope": scope,
            "target_id": target_id,
            "actor_user_id": actor_user_id,
        },
    )
    await _emit_tag_event(
        session,
        action="EXCLUDE",
        org_id=org_id,
        tag_id=tag_id,
        scope=scope,
        target_id=target_id,
        actor_user_id=actor_user_id,
        payload={"experiment_id": experiment_id},
    )

    effective_task_id = task_id or (target_id if scope == "TASK" else None)
    if effective_task_id:
        await recompute_task_browse_projection(session, task_id=effective_task_id)
        await enqueue_tag_project_worker_job(
            session,
            scope="TASK",
            target_id=effective_task_id,
            task_id=effective_task_id,
            org_id=org_id,
            mode="task_all_versions",
        )


async def unexclude_tag_core(
    session,
    *,
    tag_id: str,
    experiment_id: str,
    scope: str,
    target_id: str,
    task_id: str | None,
    org_id: str | None,
    actor_user_id: str | None,
) -> None:
    """Soft-delete the tombstone so the living tag re-applies."""
    await session.execute(
        text(
            """
            UPDATE tag_exclusions
            SET deleted_at = NOW(),
                updated_at = NOW()
            WHERE deleted_at IS NULL
              AND tag_id = :tag_id
              AND experiment_id = :exp_id
              AND scope = :scope
              AND target_id = :target_id
            """
        ),
        {
            "tag_id": tag_id,
            "exp_id": experiment_id,
            "scope": scope,
            "target_id": target_id,
        },
    )
    await _emit_tag_event(
        session,
        action="UNEXCLUDE",
        org_id=org_id,
        tag_id=tag_id,
        scope=scope,
        target_id=target_id,
        actor_user_id=actor_user_id,
        payload={"experiment_id": experiment_id},
    )

    effective_task_id = task_id or (target_id if scope == "TASK" else None)
    if effective_task_id:
        await recompute_task_browse_projection(session, task_id=effective_task_id)
        await enqueue_tag_project_worker_job(
            session,
            scope="TASK",
            target_id=effective_task_id,
            task_id=effective_task_id,
            org_id=org_id,
            mode="task_all_versions",
        )


class TagProfanityError(ValueError):
    pass


class TagReservedNamespaceError(PermissionError):
    pass


class TagPolicyError(PermissionError):
    pass


class _PolicyLike:
    """Adapter so check_tag_text can accept a plain dict OR an ORM row."""

    def __init__(self, d: dict):
        self.profanity_mode = d.get("profanity_mode", "ENFORCE")
        self.profanity_allowlist = d.get("profanity_allowlist", [])
        self.profanity_denylist = d.get("profanity_denylist", [])


async def create_tag_core(
    session,
    *,
    key: str,
    value: str | None,
    org_id: str | None,
    actor_user_id: str | None,
    policy: dict,
    is_admin: bool,
    color: str | None = None,
    description: str | None = None,
    visibility: str = "PRIVATE",
) -> str:
    """Idempotent create-or-resurrect with full governance enforcement."""
    if policy.get("who_can_create") == "ADMIN_ONLY" and not is_admin:
        raise TagPolicyError("only admins may create new tags in this org")

    if visibility not in ("PRIVATE", "PUBLIC"):
        raise TagNameError(
            f"invalid visibility {visibility!r}; expected 'PRIVATE' or 'PUBLIC'"
        )

    normalized_key = normalize_tag_key(key)
    normalized_value = normalize_tag_value(value)

    # Reserved-namespace check BEFORE charset validation: a reserved prefix
    # may use a separator (e.g. ':') outside the default charset, and a
    # non-admin must be told it's reserved rather than "bad charset".
    if (
        is_reserved_prefix(
            normalized_key,
            reserved_prefixes=list(policy.get("reserved_prefixes", []) or []),
        )
        and not is_admin
    ):
        raise TagReservedNamespaceError(
            f"key {normalized_key!r} uses a reserved prefix; admin only"
        )

    try:
        validate_tag_key(
            normalized_key,
            name_max_len=int(policy.get("name_max_len", 64)),
            charset_re=str(policy.get("name_charset", "[a-z0-9._-]")),
        )
        validate_tag_value(
            normalized_value,
            name_max_len=int(policy.get("name_max_len", 64)),
            charset_re=str(policy.get("name_charset", "[a-z0-9._-]")),
        )
    except TagNameError as exc:
        raise TagNameError(str(exc)) from exc

    profanity_result = check_tag_text(
        f"{normalized_key} {normalized_value or ''}", _PolicyLike(policy)
    )
    if not profanity_result.allowed:
        raise TagProfanityError(", ".join(profanity_result.hits))

    new_id = str(uuid.uuid4())[:16]
    rows = (
        await session.execute(
            text(
                """
                INSERT INTO tags
                    (id, org_id, key, normalized_key, value, normalized_value,
                     color, description, visibility, state, row_version,
                     owner_user_id, created_by_user_id, created_at, updated_at)
                VALUES
                    (:id, :org_id, :key, :nk, :value, :nv,
                     :color, :description, :visibility, 'ACTIVE', 1,
                     :owner, :owner, NOW(), NOW())
                ON CONFLICT (COALESCE(org_id, ''), normalized_key,
                             COALESCE(normalized_value, ''))
                    WHERE deleted_at IS NULL AND state <> 'DELETED'
                DO UPDATE SET state = CASE
                      WHEN tags.state = 'ARCHIVED' THEN 'ACTIVE'
                      ELSE tags.state END,
                    row_version = tags.row_version + 1,
                    updated_at = NOW()
                RETURNING id
                """
            ),
            {
                "id": new_id,
                "org_id": org_id,
                "key": key,
                "nk": normalized_key,
                "value": value,
                "nv": normalized_value,
                "color": color,
                "description": description,
                "visibility": visibility,
                "owner": actor_user_id,
            },
        )
    ).all()
    resolved_id = str(rows[0][0]) if rows else new_id
    await _emit_tag_event(
        session,
        action="CREATE",
        org_id=org_id,
        tag_id=resolved_id,
        scope=None,
        target_id=None,
        actor_user_id=actor_user_id,
        payload={"normalized_key": normalized_key, "visibility": visibility},
    )
    return resolved_id


class TagConcurrencyError(RuntimeError):
    pass


class TagMergeChainError(ValueError):
    pass


async def _optimistic_state_update(
    session,
    *,
    tag_id: str,
    new_state: str,
    allowed_from: list[str],
    expected_row_version: int,
) -> None:
    # The CAST is load-bearing: asyncpg deduces ONE type per parameter, and
    # :new_state appears both as the state assignment (enum tag_state) and in
    # the CASE comparison below (text) — without the explicit cast the
    # prepare fails with AmbiguousParameterError.
    result = await session.execute(
        text(
            """
            UPDATE tags
            SET state = CAST(:new_state AS tag_state),
                row_version = row_version + 1,
                updated_at = NOW(),
                deleted_at = CASE
                    WHEN :new_state = 'DELETED' THEN NOW()
                    ELSE deleted_at
                END
            WHERE id = :tag_id
              AND row_version = :expected_row_version
              AND state::text = ANY(:allowed_from)
            """
        ),
        {
            "new_state": new_state,
            "tag_id": tag_id,
            "expected_row_version": expected_row_version,
            "allowed_from": list(allowed_from),
        },
    )
    if (getattr(result, "rowcount", 0) or 0) == 0:
        raise TagConcurrencyError(
            f"tag {tag_id} could not transition to {new_state}: "
            f"row_version mismatch or illegal source state"
        )


async def archive_tag_core(
    session,
    *,
    tag_id: str,
    org_id: str | None,
    actor_user_id: str | None,
    expected_row_version: int,
) -> None:
    await _optimistic_state_update(
        session,
        tag_id=tag_id,
        new_state="ARCHIVED",
        allowed_from=["ACTIVE"],
        expected_row_version=expected_row_version,
    )
    await _emit_tag_event(
        session,
        action="ARCHIVE",
        org_id=org_id,
        tag_id=tag_id,
        scope=None,
        target_id=None,
        actor_user_id=actor_user_id,
    )


async def unarchive_tag_core(
    session,
    *,
    tag_id: str,
    org_id: str | None,
    actor_user_id: str | None,
    expected_row_version: int,
) -> None:
    await _optimistic_state_update(
        session,
        tag_id=tag_id,
        new_state="ACTIVE",
        allowed_from=["ARCHIVED"],
        expected_row_version=expected_row_version,
    )
    await _emit_tag_event(
        session,
        action="UNARCHIVE",
        org_id=org_id,
        tag_id=tag_id,
        scope=None,
        target_id=None,
        actor_user_id=actor_user_id,
    )


async def delete_tag_core(
    session,
    *,
    tag_id: str,
    org_id: str | None,
    actor_user_id: str | None,
    expected_row_version: int,
    cascade_remove_assignments: bool = False,
) -> None:
    if not cascade_remove_assignments:
        n = await session.scalar(
            text(
                """
                SELECT COUNT(*) FROM tag_assignments
                WHERE deleted_at IS NULL
                  AND state = 'ACTIVE'
                  AND tag_id = :tag_id
                """
            ),
            {"tag_id": tag_id},
        )
        if (n or 0) > 0:
            raise TagPolicyError(
                f"tag {tag_id} still has {n} ACTIVE assignments; "
                "archive first or call with cascade_remove_assignments=True"
            )

    # Optimistic state transition FIRST — gates everything below.
    await _optimistic_state_update(
        session,
        tag_id=tag_id,
        new_state="DELETED",
        allowed_from=["ACTIVE", "ARCHIVED"],
        expected_row_version=expected_row_version,
    )

    if cascade_remove_assignments:
        removed = (
            await session.execute(
                text(
                    """
                    UPDATE tag_assignments
                    SET state = 'REMOVED',
                        row_version = row_version + 1,
                        removed_at = NOW(),
                        updated_at = NOW()
                    WHERE deleted_at IS NULL
                      AND state = 'ACTIVE'
                      AND tag_id = :tag_id
                    RETURNING id, task_id, scope, target_id
                    """
                ),
                {"tag_id": tag_id},
            )
        ).all()
        affected_task_ids: set[str] = set()
        for _aid, atask_id, ascope, atarget in removed:
            await _emit_tag_event(
                session,
                action="REMOVE",
                org_id=org_id,
                tag_id=tag_id,
                scope=str(ascope) if ascope is not None else None,
                target_id=str(atarget) if atarget is not None else None,
                actor_user_id=actor_user_id,
            )
            sc = str(ascope) if ascope is not None else None
            if sc == "EXPERIMENT" and atarget:
                await enqueue_tag_project_worker_job(
                    session,
                    scope="EXPERIMENT",
                    target_id=str(atarget),
                    task_id=None,
                    org_id=org_id,
                    mode="experiment_living_fanout",
                )
            else:
                resolved_task = atask_id or (atarget if sc == "TASK" else None)
                if resolved_task:
                    affected_task_ids.add(str(resolved_task))
        for tid in affected_task_ids:
            await recompute_task_browse_projection(session, task_id=tid)
            await enqueue_tag_project_worker_job(
                session,
                scope="TASK",
                target_id=tid,
                task_id=tid,
                org_id=org_id,
                mode="task_all_versions",
            )

    await _emit_tag_event(
        session,
        action="DELETE",
        org_id=org_id,
        tag_id=tag_id,
        scope=None,
        target_id=None,
        actor_user_id=actor_user_id,
    )


async def merge_tag_core(
    session,
    *,
    source_tag_id: str,
    target_tag_id: str,
    org_id: str | None,
    actor_user_id: str | None,
    expected_row_version: int | None = None,
) -> None:
    """Mark ``source`` MERGED → ``target``. Forbids chains.

    Existing assignments stay attached to the source row; reads follow
    ``merged_into_id`` to the survivor. The source row stays as a
    tombstone so historical filters and saved-filter persisted tag ids
    keep resolving.
    """
    if source_tag_id == target_tag_id:
        raise TagMergeChainError("cannot merge a tag into itself")
    # Lock source + target rows together in deterministic id order so two
    # concurrent merges involving the same tag serialize and re-evaluate
    # the chain guards under the lock.
    locked = (
        await session.execute(
            text(
                """
                SELECT id, state::text AS state
                FROM tags
                WHERE id IN (:source_id, :target_id)
                ORDER BY id
                FOR UPDATE
                """
            ),
            {"source_id": source_tag_id, "target_id": target_tag_id},
        )
    ).all()
    states = {str(r[0]): str(r[1]) for r in locked}
    target_state = states.get(target_tag_id)
    source_state = states.get(source_tag_id)

    if target_state == "MERGED":
        raise TagMergeChainError(
            f"target tag {target_tag_id} is itself MERGED; chained merges forbidden"
        )
    if target_state != "ACTIVE":
        raise TagPolicyError(
            f"merge target {target_tag_id} must be ACTIVE (got {target_state})"
        )
    if source_state not in ("ACTIVE", "ARCHIVED"):
        raise TagConcurrencyError(
            f"merge source {source_tag_id} is not mergeable (state {source_state})"
        )
    inbound = await session.scalar(
        text(
            """
            SELECT 1 FROM tags
            WHERE merged_into_id = :source_id
              AND state = 'MERGED'
            LIMIT 1
            """
        ),
        {"source_id": source_tag_id},
    )
    if inbound:
        raise TagMergeChainError(
            f"tag {source_tag_id} already has tags merged into it; "
            "merging it would create a forbidden chain"
        )
    params: dict[str, object] = {
        "source_id": source_tag_id,
        "target_id": target_tag_id,
    }
    rv_clause = ""
    if expected_row_version is not None:
        rv_clause = " AND row_version = :erv"
        params["erv"] = expected_row_version
    result = await session.execute(
        text(
            f"""
            UPDATE tags
            SET state = 'MERGED',
                merged_into_id = :target_id,
                row_version = row_version + 1,
                updated_at = NOW()
            WHERE id = :source_id
              AND state IN ('ACTIVE', 'ARCHIVED'){rv_clause}
            """
        ),
        params,
    )
    if (getattr(result, "rowcount", 0) or 0) == 0:
        raise TagConcurrencyError(
            f"merge source {source_tag_id} not mergeable (wrong state or row_version)"
        )
    await _emit_tag_event(
        session,
        action="MERGE",
        org_id=org_id,
        tag_id=source_tag_id,
        scope=None,
        target_id=target_tag_id,
        actor_user_id=actor_user_id,
        payload={"merged_into_id": target_tag_id},
    )
    affected = (
        await session.execute(
            text(
                """
                SELECT DISTINCT scope::text AS scope, target_id, task_id
                FROM tag_assignments
                WHERE deleted_at IS NULL
                  AND state = 'ACTIVE'
                  AND tag_id = :source_id
                """
            ),
            {"source_id": source_tag_id},
        )
    ).all()
    seen_tasks: set[str] = set()
    seen_experiments: set[str] = set()
    for sc, tgt, tsk in affected:
        if sc == "EXPERIMENT":
            if tgt and str(tgt) not in seen_experiments:
                seen_experiments.add(str(tgt))
                await enqueue_tag_project_worker_job(
                    session,
                    scope="EXPERIMENT",
                    target_id=str(tgt),
                    task_id=None,
                    org_id=org_id,
                    mode="experiment_living_fanout",
                )
        else:
            resolved_task = tsk or (tgt if sc == "TASK" else None)
            if resolved_task and str(resolved_task) not in seen_tasks:
                seen_tasks.add(str(resolved_task))
                await recompute_task_browse_projection(
                    session, task_id=str(resolved_task)
                )
                await enqueue_tag_project_worker_job(
                    session,
                    scope="TASK",
                    target_id=str(resolved_task),
                    task_id=str(resolved_task),
                    org_id=org_id,
                    mode="task_all_versions",
                )


async def edit_tag_core(
    session,
    *,
    tag_id: str,
    org_id: str | None,
    actor_user_id: str | None,
    expected_row_version: int,
    updates: dict,
    policy: dict | None = None,
    is_admin: bool = True,
) -> None:
    columns_allowed = {"key", "color", "description"}
    set_pairs: list[str] = []
    params: dict[str, object] = {
        "tag_id": tag_id,
        "expected_row_version": expected_row_version,
    }
    for k, v in updates.items():
        if k not in columns_allowed:
            raise ValueError(f"cannot EDIT field {k}")
        if k == "key":
            normalized = normalize_tag_key(str(v))
            if policy is not None:
                validate_tag_key(
                    normalized,
                    name_max_len=int(policy.get("name_max_len", 64)),
                    charset_re=str(policy.get("name_charset", "[a-z0-9._-]")),
                )
                if (
                    is_reserved_prefix(
                        normalized,
                        reserved_prefixes=list(
                            policy.get("reserved_prefixes", []) or []
                        ),
                    )
                    and not is_admin
                ):
                    raise TagReservedNamespaceError(
                        f"key {normalized!r} uses a reserved prefix; admin only"
                    )
                pr = check_tag_text(normalized, _PolicyLike(policy))
                if not pr.allowed:
                    raise TagProfanityError(", ".join(pr.hits))
            params["normalized_key"] = normalized
            set_pairs.append("normalized_key = :normalized_key")
        params[k] = v
        set_pairs.append(f"{k} = :{k}")
    if not set_pairs:
        return
    set_pairs.append("row_version = row_version + 1")
    set_pairs.append("updated_at = NOW()")
    result = await session.execute(
        text(
            f"""
            UPDATE tags
            SET {", ".join(set_pairs)}
            WHERE id = :tag_id
              AND row_version = :expected_row_version
            """
        ),
        params,
    )
    if (getattr(result, "rowcount", 0) or 0) == 0:
        raise TagConcurrencyError(f"tag {tag_id} row_version mismatch on EDIT")
    await _emit_tag_event(
        session,
        action="EDIT" if "key" not in updates else "RENAME",
        org_id=org_id,
        tag_id=tag_id,
        scope=None,
        target_id=None,
        actor_user_id=actor_user_id,
        payload={k: v for k, v in updates.items() if k != "expected_row_version"},
    )


async def set_tag_visibility_core(
    session,
    *,
    tag_id: str,
    org_id: str | None,
    actor_user_id: str | None,
    new_visibility: str,
    expected_row_version: int,
) -> None:
    if new_visibility not in {"PRIVATE", "PUBLIC"}:
        # TagNameError maps to HTTP 400 in the router (plain ValueError
        # would surface as a 500).
        raise TagNameError(f"bad visibility: {new_visibility!r}")
    result = await session.execute(
        text(
            """
            UPDATE tags
            SET visibility = :new_visibility,
                row_version = row_version + 1,
                updated_at = NOW()
            WHERE id = :tag_id
              AND row_version = :expected_row_version
            """
        ),
        {
            "tag_id": tag_id,
            "expected_row_version": expected_row_version,
            "new_visibility": new_visibility,
        },
    )
    if (getattr(result, "rowcount", 0) or 0) == 0:
        raise TagConcurrencyError(
            f"tag {tag_id} row_version mismatch on SET_VISIBILITY"
        )
    await _emit_tag_event(
        session,
        action="SET_VISIBILITY",
        org_id=org_id,
        tag_id=tag_id,
        scope=None,
        target_id=None,
        actor_user_id=actor_user_id,
        payload={"visibility": new_visibility},
    )


async def grant_tag_capability_core(
    session,
    *,
    tag_id: str,
    org_id: str | None,
    actor_user_id: str | None,
    principal_type: str,
    principal_user_id: str | None,
    capability: str,
) -> str:
    if capability not in {"RENAME", "MERGE", "DELETE", "EDIT"}:
        raise ValueError(f"capability {capability} is not delegatable")
    new_id = str(uuid.uuid4())[:16]
    rows = (
        await session.execute(
            text(
                """
                INSERT INTO tag_grants
                    (id, tag_id, org_id, principal_type, principal_user_id,
                     capability, granted_by_user_id, created_at, updated_at)
                VALUES
                    (:id, :tag_id, :org_id, :principal_type, :principal_user_id,
                     :capability, :actor, NOW(), NOW())
                ON CONFLICT (tag_id, principal_type,
                             COALESCE(principal_user_id, ''), capability)
                    WHERE deleted_at IS NULL
                DO UPDATE SET updated_at = NOW(),
                              deleted_at = NULL
                RETURNING id
                """
            ),
            {
                "id": new_id,
                "tag_id": tag_id,
                "org_id": org_id,
                "principal_type": principal_type,
                "principal_user_id": principal_user_id,
                "capability": capability,
                "actor": actor_user_id,
            },
        )
    ).all()
    await _emit_tag_event(
        session,
        action="GRANT",
        org_id=org_id,
        tag_id=tag_id,
        scope=None,
        target_id=None,
        actor_user_id=actor_user_id,
        payload={
            "principal_type": principal_type,
            "principal_user_id": principal_user_id,
            "capability": capability,
        },
    )
    return str(rows[0][0]) if rows else new_id


async def revoke_tag_capability_core(
    session,
    *,
    grant_id: str,
    tag_id: str,
    org_id: str | None,
    actor_user_id: str | None,
) -> None:
    await session.execute(
        text(
            """
            UPDATE tag_grants
            SET deleted_at = NOW(),
                updated_at = NOW()
            WHERE id = :grant_id
              AND tag_id = :tag_id
              AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
              AND deleted_at IS NULL
            """
        ),
        {"grant_id": grant_id, "tag_id": tag_id, "org_id": org_id},
    )
    await _emit_tag_event(
        session,
        action="REVOKE",
        org_id=org_id,
        tag_id=tag_id,
        scope=None,
        target_id=None,
        actor_user_id=actor_user_id,
        payload={"grant_id": grant_id},
    )

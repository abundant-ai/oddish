"""Recompute-from-truth materialization for the tag projection.

Every tag write enqueues a sibling ``worker_jobs(kind=TAG_PROJECT)`` row in
the same transaction. The handler in ``tag_project_handler.py`` then calls
into this module to recompute one target's projected ``effective_tag_ids``
from the truth tables (``tag_assignments`` / ``tag_exclusions`` /
``task_experiments`` / ``tags``).

Recompute-from-truth makes every run idempotent and order-independent;
there is intentionally no last_event_id watermark (bigserial != commit
order).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text


async def _query_effective_tag_ids_for_version(
    session,
    task_id: str,
    version_id: str,
) -> set[str]:
    """Return the set of effective tag IDs for ``(task_id, version_id)``.

    Mirrors the resolution rule:
        VERSION assignments on V
        ∪ TASK assignments on T
        ∪ living EXPERIMENT assignments for experiments E where
          (T,E) ∈ task_experiments AND task_experiments.deleted_at IS NULL
        − tag_exclusions matching (E, tag, T or V)
        − tags where state = 'DELETED'
        — and merged tags follow merged_into_id to the survivor.
    """
    rows = (
        await session.execute(
            text(
                """
                WITH base AS (
                    SELECT a.tag_id
                    FROM tag_assignments a
                    WHERE a.deleted_at IS NULL
                      AND a.state = 'ACTIVE'
                      AND a.scope = 'VERSION'
                      AND a.target_id = :version_id
                    UNION
                    SELECT a.tag_id
                    FROM tag_assignments a
                    WHERE a.deleted_at IS NULL
                      AND a.state = 'ACTIVE'
                      AND a.scope = 'TASK'
                      AND a.target_id = :task_id
                    UNION
                    SELECT a.tag_id
                    FROM tag_assignments a
                    JOIN task_experiments te ON te.experiment_id = a.target_id
                    WHERE a.deleted_at IS NULL
                      AND a.state = 'ACTIVE'
                      AND a.scope = 'EXPERIMENT'
                      AND a.source = 'EXPERIMENT_LIVING'
                      AND te.task_id = :task_id
                      AND te.deleted_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM tag_exclusions x
                          WHERE x.deleted_at IS NULL
                            AND x.tag_id = a.tag_id
                            AND x.experiment_id = a.target_id
                            AND (
                                (x.scope = 'TASK' AND x.target_id = :task_id)
                             OR (x.scope = 'VERSION' AND x.target_id = :version_id)
                            )
                      )
                )
                SELECT COALESCE(t.merged_into_id, base.tag_id) AS resolved_tag_id
                FROM base
                JOIN tags t ON t.id = base.tag_id
                WHERE t.state <> 'DELETED'
                """
            ),
            {"task_id": task_id, "version_id": version_id},
        )
    ).all()
    return {row[0] for row in rows}


async def recompute_version_effective_tags(
    session,
    *,
    task_id: str,
    version_id: str,
) -> set[str]:
    """Write a freshly-recomputed array to ``task_versions.effective_tag_ids``."""
    tag_ids = await _query_effective_tag_ids_for_version(
        session, task_id=task_id, version_id=version_id
    )
    await session.execute(
        text(
            """
            UPDATE task_versions
            SET effective_tag_ids = CAST(:ids AS TEXT[])
            WHERE id = :version_id
            """
        ),
        {"ids": sorted(tag_ids), "version_id": version_id},
    )
    return tag_ids


async def _union_effective_tag_ids_for_task(
    session, *, task_id: str, version_ids: list[str]
) -> set[str]:
    """``tasks.effective_tag_ids`` is the UNION of every version's
    resolved effective set. A VERSION-scope exclusion drops
    the tag for that one version only — if any sibling version still
    inherits it, it is effective on the task.
    """
    union: set[str] = set()
    for vid in version_ids:
        union |= await _query_effective_tag_ids_for_version(
            session, task_id=task_id, version_id=vid
        )
    return union


async def recompute_task_browse_projection(session, *, task_id: str) -> None:
    """Recompute ``tasks.effective_tag_ids`` and ``tasks.current_version_tag_ids``.

    Computes ``tasks.effective_tag_ids`` as the **union** of each
    version's resolved effective set (see ``_query_effective_tag_ids_for_version``).
    This is the correct read-projection semantics: a tag
    belongs to the task if it is effective on AT LEAST ONE version, so a
    VERSION-scope exclusion can only remove the tag for that version, not
    for the task as a whole.

    Runs synchronously from the request path on apply/unapply so the user
    sees the chip appear/disappear immediately; the per-version
    ``task_versions.effective_tag_ids`` arrays are filled asynchronously
    by ``recompute_all_versions_for_task`` from the worker.
    """
    rows = (
        await session.execute(
            text("SELECT id FROM task_versions WHERE task_id = :task_id"),
            {"task_id": task_id},
        )
    ).all()
    version_ids = [str(r[0]) for r in rows]

    union_ids = await _union_effective_tag_ids_for_task(
        session, task_id=task_id, version_ids=version_ids
    )
    await session.execute(
        text(
            """
            UPDATE tasks
            SET effective_tag_ids = CAST(:ids AS TEXT[])
            WHERE id = :task_id
            """
        ),
        {"task_id": task_id, "ids": sorted(union_ids)},
    )

    current_version_id = await session.scalar(
        text("SELECT current_version_id FROM tasks WHERE id = :task_id"),
        {"task_id": task_id},
    )
    if current_version_id:
        ids = await _query_effective_tag_ids_for_version(
            session, task_id=task_id, version_id=str(current_version_id)
        )
    else:
        ids = set()
    await session.execute(
        text(
            """
            UPDATE tasks
            SET current_version_tag_ids = CAST(:ids AS TEXT[])
            WHERE id = :task_id
            """
        ),
        {"task_id": task_id, "ids": sorted(ids)},
    )


async def recompute_all_versions_for_task(
    session, *, task_id: str, chunk_size: int = 200
) -> int:
    """Recompute ``task_versions.effective_tag_ids`` for every version of a task.

    Returns the number of versions processed. Chunked to keep the txn small.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id FROM task_versions WHERE task_id = :task_id ORDER BY version"
            ),
            {"task_id": task_id},
        )
    ).all()
    version_ids = [str(r[0]) for r in rows]
    count = 0
    for start in range(0, len(version_ids), chunk_size):
        for version_id in version_ids[start : start + chunk_size]:
            await recompute_version_effective_tags(
                session, task_id=task_id, version_id=version_id
            )
            count += 1
        await session.flush()
    return count


@dataclass
class UserTagView:
    """Materialized view of a tag attached to a task, for the API DTOs.

    ``current=True`` means the tag is on the latest task version (primary
    chip); ``older=True`` means the tag is effective only on older
    versions (de-emphasized chip).
    """

    tag_id: str
    key: str
    value: str | None
    color: str | None
    visibility: str
    current: bool
    older: bool


async def list_direct_target_tags(
    session,
    *,
    scope: str,
    target_id: str,
) -> list[UserTagView]:
    """Direct tags assigned at exactly (scope, target_id) — e.g. an
    experiment's own tags for its header editor. Merged tags follow
    ``merged_into_id``; DELETED tags are dropped.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT t.id AS tag_id, t.key, t.value, t.color, t.visibility
                FROM tag_assignments ta
                JOIN tags t0 ON t0.id = ta.tag_id
                JOIN tags t ON t.id = COALESCE(t0.merged_into_id, t0.id)
                WHERE ta.scope = CAST(:scope AS tag_assignment_scope)
                  AND ta.state = 'ACTIVE'
                  AND ta.deleted_at IS NULL
                  AND ta.target_id = :target_id
                  AND t.deleted_at IS NULL
                  AND t.state <> 'DELETED'
                ORDER BY t.key
                """
            ),
            {"scope": scope, "target_id": target_id},
        )
    ).all()
    return [
        UserTagView(
            tag_id=str(tag_id),
            key=str(key),
            value=value,
            color=color,
            visibility=str(visibility),
            current=True,
            older=False,
        )
        for tag_id, key, value, color, visibility in rows
    ]


async def list_direct_tags_for_targets(
    session,
    *,
    scope: str,
    target_ids: list[str],
) -> dict[str, list[UserTagView]]:
    """Batch variant of ``list_direct_target_tags`` for list surfaces (e.g.
    dashboard experiment rows). Returns target_id -> views; targets with no
    tags are absent. Merged tags follow ``merged_into_id``; DELETED dropped.
    """
    if not target_ids:
        return {}
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ta.target_id,
                       t.id AS tag_id, t.key, t.value, t.color, t.visibility
                FROM tag_assignments ta
                JOIN tags t0 ON t0.id = ta.tag_id
                JOIN tags t ON t.id = COALESCE(t0.merged_into_id, t0.id)
                WHERE ta.scope = CAST(:scope AS tag_assignment_scope)
                  AND ta.state = 'ACTIVE'
                  AND ta.deleted_at IS NULL
                  AND ta.target_id = ANY(:target_ids)
                  AND t.deleted_at IS NULL
                  AND t.state <> 'DELETED'
                ORDER BY ta.target_id, t.key
                """
            ),
            {"scope": scope, "target_ids": list(target_ids)},
        )
    ).all()
    out: dict[str, list[UserTagView]] = {}
    for target_id, tag_id, key, value, color, visibility in rows:
        out.setdefault(str(target_id), []).append(
            UserTagView(
                tag_id=str(tag_id),
                key=str(key),
                value=value,
                color=color,
                visibility=str(visibility),
                current=True,
                older=False,
            )
        )
    return out


async def list_direct_version_tags(
    session,
    *,
    version_ids: list[str],
) -> dict[str, list[UserTagView]]:
    """Direct VERSION-scope tags per version id — the version's own chips,
    as opposed to the task-level effective resolution. Merged tags follow
    ``merged_into_id``; DELETED tags are dropped.
    """
    if not version_ids:
        return {}
    rows = (
        await session.execute(
            text(
                """
                SELECT ta.target_id AS version_id,
                       t.id AS tag_id, t.key, t.value, t.color, t.visibility
                FROM tag_assignments ta
                JOIN tags t0 ON t0.id = ta.tag_id
                JOIN tags t ON t.id = COALESCE(t0.merged_into_id, t0.id)
                WHERE ta.scope = 'VERSION'
                  AND ta.state = 'ACTIVE'
                  AND ta.deleted_at IS NULL
                  AND ta.target_id = ANY(:version_ids)
                  AND t.deleted_at IS NULL
                  AND t.state <> 'DELETED'
                ORDER BY t.key
                """
            ),
            {"version_ids": list(version_ids)},
        )
    ).all()
    out: dict[str, list[UserTagView]] = {}
    for version_id, tag_id, key, value, color, visibility in rows:
        out.setdefault(str(version_id), []).append(
            UserTagView(
                tag_id=str(tag_id),
                key=str(key),
                value=value,
                color=color,
                visibility=str(visibility),
                current=True,
                older=False,
            )
        )
    return out


async def list_effective_user_tags_for_task_versions(
    session,
    *,
    task_ids: list[str],
    public_only: bool = False,
) -> dict[str, list[UserTagView]]:
    """For each task id, return the union of tags on its current version
    and its older-only tags. Merged tags follow ``merged_into_id``.

    ``public_only=True`` filters to ``tags.visibility='PUBLIC'`` — used by
    the public share/dataset endpoints.
    """
    if not task_ids:
        return {}
    visibility_clause = "AND tag.visibility = 'PUBLIC'" if public_only else ""
    rows = (
        await session.execute(
            text(
                f"""
                WITH tcv AS (
                    SELECT id AS task_id, current_version_id
                    FROM tasks
                    WHERE id = ANY(:task_ids)
                ),
                effective AS (
                    SELECT
                        tcv.task_id,
                        tag.id AS tag_id,
                        tag.key,
                        tag.value,
                        tag.color,
                        tag.visibility::text AS visibility,
                        (tag.id = ANY(t.current_version_tag_ids)) AS current_flag,
                        (
                            (tag.id = ANY(t.effective_tag_ids))
                            AND NOT (tag.id = ANY(t.current_version_tag_ids))
                        ) AS older_flag
                    FROM tasks t
                    JOIN tcv ON tcv.task_id = t.id
                    JOIN LATERAL unnest(t.effective_tag_ids) AS u(eff_id) ON TRUE
                    JOIN tags raw ON raw.id = u.eff_id
                    JOIN tags tag ON tag.id = COALESCE(raw.merged_into_id, raw.id)
                    WHERE tag.state <> 'DELETED'
                      {visibility_clause}
                )
                SELECT DISTINCT task_id, tag_id, key, value, color, visibility,
                                bool_or(current_flag) OVER (PARTITION BY task_id, tag_id),
                                bool_or(older_flag)   OVER (PARTITION BY task_id, tag_id)
                FROM effective
                """
            ),
            {"task_ids": task_ids},
        )
    ).all()
    out: dict[str, list[UserTagView]] = {tid: [] for tid in task_ids}
    seen: set[tuple[str, str]] = set()
    for tid, tag_id, key, value, color, visibility, current_flag, older_flag in rows:
        if (str(tid), str(tag_id)) in seen:
            continue
        seen.add((str(tid), str(tag_id)))
        out.setdefault(str(tid), []).append(
            UserTagView(
                tag_id=str(tag_id),
                key=str(key),
                value=str(value) if value is not None else None,
                color=str(color) if color is not None else None,
                visibility=str(visibility),
                current=bool(current_flag),
                older=bool(older_flag),
            )
        )
    return out

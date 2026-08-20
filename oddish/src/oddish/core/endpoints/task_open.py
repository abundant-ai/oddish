"""Bounded task-page first paint without task/trial ORM hydration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.endpoints.task_open_aggregates import fold_task_open_groups
from oddish.core.endpoints.task_open_builders import (
    TASK_OPEN_TRIAL_LIMIT,
    bounded_text,
    compact_verdict,
    experiments,
    github_meta,
    tags,
    trial_refs,
)
from oddish.core.endpoints.task_open_queries import (
    AGGREGATE_SQL,
    IDENTITY_SQL,
    PREVIEW_SQL,
)
from oddish.db import TaskStatus
from oddish.schemas import (
    TaskOpenResponse,
    TaskOpenTask,
    TaskOpenVersionRef,
)
from oddish.timing import TimingRecorder, elapsed_ms, now


async def get_task_open_core(
    session: AsyncSession,
    *,
    task_id: str,
    version_id: str | None = None,
    org_id: str | None = None,
    record_timing: TimingRecorder | None = None,
) -> TaskOpenResponse:
    """Return one bounded task/version shell in at most three SQL statements."""
    params = {"task_id": task_id, "version_id": version_id, "org_id": org_id}
    started = now()
    identity = (await session.execute(IDENTITY_SQL, params)).mappings().one_or_none()
    if record_timing:
        record_timing(
            "task_open_identity", elapsed_ms(started), "Task open scalar identity"
        )
    if identity is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if version_id is not None and identity["selected_version_id"] is None:
        raise HTTPException(
            status_code=404, detail=f"Version {version_id} not found for task {task_id}"
        )

    params.update(
        version_id=identity["selected_version_id"],
        current_version_id=identity["current_version_id"],
    )
    started = now()
    aggregate = (await session.execute(AGGREGATE_SQL, params)).mappings().one()
    if record_timing:
        record_timing(
            "task_open_aggregates", elapsed_ms(started), "Task open aggregate query"
        )
    totals, selected, current_counts = fold_task_open_groups(
        list(aggregate["groups"] or []),
        identity,
        float(aggregate["qa_cost_usd"] or 0.0),
    )
    if selected is not None:
        selected.user_tags = tags(identity["selected_version_tags"])
        selected.experiments = experiments(aggregate["experiments"])

    preview_rows: list[Mapping[str, Any]] = []
    if identity["selected_version_id"] is not None:
        started = now()
        result = await session.execute(PREVIEW_SQL, params)
        preview_rows = list(result.mappings().all())
        if record_timing:
            record_timing(
                "task_open_preview",
                elapsed_ms(started),
                "Task open capped trial preview",
            )

    status = identity["status"]
    current_total, current_terminal = current_counts
    if current_total and current_terminal >= current_total:
        status = TaskStatus.COMPLETED
    legacy_tags = identity["tags"] if isinstance(identity["tags"], dict) else {}
    default = None
    if identity["default_version_id"] is not None:
        default = TaskOpenVersionRef(
            id=str(identity["default_version_id"]),
            version=int(identity["default_version"]),
            message=identity["default_version_message"],
            created_at=identity["default_version_created_at"],
            is_current=True,
        )
    return TaskOpenResponse(
        task=TaskOpenTask(
            id=str(identity["task_id"]),
            name=str(identity["name"]),
            status=status,
            priority=identity["priority"],
            user=str(identity["user"]),
            github_username=legacy_tags.get("github_username"),
            github_meta=github_meta(legacy_tags),
            link=identity["link"],
            task_path=str(identity["task_path"]),
            experiments=experiments(identity["experiments"]),
            current_version=identity["default_version"],
            current_version_id=identity["current_version_id"],
            user_tags=tags(identity["task_tags"]),
            run_analysis=bool(identity["run_analysis"]),
            verdict_status=identity["verdict_status"],
            verdict=compact_verdict(identity["verdict"]),
            verdict_error=bounded_text(identity["verdict_error"]),
            created_at=identity["created_at"],
            updated_at=identity["updated_at"],
        ),
        default_version=default,
        selected_version=selected,
        totals=totals,
        trials=trial_refs(preview_rows),
        trials_has_more=len(preview_rows) > TASK_OPEN_TRIAL_LIMIT,
    )

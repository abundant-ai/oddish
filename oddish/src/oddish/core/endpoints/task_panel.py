"""Task panel metadata and action availability, without trial payloads/history."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.schemas import TaskPanelResponse, TaskStatusResponse, TaskVersionSummary


async def get_task_panel_core(
    session: AsyncSession,
    *,
    task_id: str,
    version: int | None = None,
    org_id: str | None = None,
) -> TaskPanelResponse:
    result = await session.execute(
        text("""
        SELECT t.id, t.name, lower(t.status::text) AS status,
          lower(t.priority::text) AS priority, t."user", t.task_path,
          t.verdict, lower(t.verdict_status::text) AS verdict_status,
          t.verdict_error, t.run_analysis, t.current_version_id, t.created_at, t.updated_at,
          dv.version AS current_version, v.id AS version_id, v.version,
          v.content_hash, v.created_at AS version_created_at,
          v.pre_trial, lower(v.pre_trial_status::text) AS pre_trial_status,
          v.pre_trial_error
        FROM tasks t
        LEFT JOIN task_versions dv ON dv.id = t.current_version_id
          AND dv.task_id = t.id AND dv.deleted_at IS NULL
        LEFT JOIN task_versions v ON v.task_id = t.id AND v.deleted_at IS NULL
          AND CASE WHEN CAST(:version AS integer) IS NULL
            THEN v.id = t.current_version_id ELSE v.version = :version END
        WHERE t.id = :task_id AND t.deleted_at IS NULL
          AND (CAST(:org_id AS text) IS NULL OR t.org_id = :org_id)
    """),
        dict(task_id=task_id, version=version, org_id=org_id),
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(404, f"Task {task_id} not found")
    if version is not None and row["version_id"] is None:
        raise HTTPException(404, f"Version {version} not found for task {task_id}")

    result = await session.execute(
        text("""
        WITH trials_for_task AS (
          SELECT tr.id, tr.kind, tr.status, tr.analysis_status,
            tr.analysis IS NOT NULL AS has_analysis, tr.task_version_id, tr.reward
          FROM trials tr WHERE tr.task_id = :task_id AND tr.deleted_at IS NULL
            AND tr.superseded_by_trial_id IS NULL AND tr.kind <> 'qa_eval'
            AND (tr.idempotency_key IS NULL OR tr.idempotency_key NOT LIKE 'combine:%')
            AND (CAST(:org_id AS text) IS NULL OR tr.org_id = :org_id OR tr.org_id IS NULL)
        ), live_jobs AS (
          SELECT j.kind, NULL::text AS trial_kind FROM worker_jobs j
          WHERE j.subject_table = 'tasks' AND j.subject_id = :task_id
            AND j.deleted_at IS NULL
            AND j.status IN ('QUEUED', 'RUNNING', 'RETRYING', 'BLOCKED')
          UNION ALL
          SELECT j.kind, tr.kind AS trial_kind FROM trials_for_task tr JOIN worker_jobs j
            ON j.subject_table = 'trials' AND j.subject_id = tr.id
          WHERE j.deleted_at IS NULL
            AND j.status IN ('QUEUED', 'RUNNING', 'RETRYING', 'BLOCKED')
        )
        SELECT count(*) AS total,
          count(*) FILTER (WHERE status = 'SUCCESS') AS completed,
          count(*) FILTER (WHERE status = 'FAILED') AS failed,
          count(*) FILTER (WHERE status = 'SKIPPED') AS skipped,
          count(*) FILTER (WHERE kind = 'agent' AND status IN
            ('PENDING', 'QUEUED', 'RUNNING', 'PAUSED', 'RETRYING')) AS active_trials,
          count(*) FILTER (WHERE kind = 'agent' AND status IN ('SUCCESS', 'FAILED')) > 0 AS can_retry,
          COALESCE(bool_or(kind = 'qa' AND status IN
            ('PENDING', 'QUEUED', 'RUNNING', 'PAUSED', 'RETRYING')), false) AS qa_active,
          COALESCE(bool_or(kind IN ('qa', 'audit') AND status IN
            ('PENDING', 'QUEUED', 'RUNNING', 'PAUSED', 'RETRYING')), false) AS analysis_trial_active,
          COALESCE(bool_or(analysis_status IN ('PENDING', 'QUEUED', 'RUNNING')), false) AS analysis_active,
          COALESCE(bool_or(has_analysis OR analysis_status IS NOT NULL), false) AS has_analysis,
          EXISTS (SELECT 1 FROM live_jobs) AS jobs_active,
          EXISTS (SELECT 1 FROM live_jobs WHERE kind = 'TRIAL' AND trial_kind = 'agent') AS trial_jobs_active,
          EXISTS (SELECT 1 FROM live_jobs WHERE kind = 'QA') AS qa_jobs_active,
          EXISTS (SELECT 1 FROM live_jobs WHERE kind = 'ANALYSIS') AS analysis_jobs_active,
          sum(reward) FILTER (WHERE task_version_id = :version_id) AS reward_sum,
          count(reward) FILTER (WHERE task_version_id = :version_id) AS reward_total
        FROM trials_for_task
    """),
        dict(task_id=task_id, version_id=row["version_id"], org_id=org_id),
    )
    counts = result.mappings().one()
    qa_active = (
        counts["qa_active"]
        or counts["qa_jobs_active"]
        or row["status"] == "verdict_pending"
        or row["verdict_status"] in ("pending", "queued", "running")
    )
    analysis_active = (
        counts["analysis_active"]
        or counts["analysis_jobs_active"]
        or counts["analysis_trial_active"]
        or row["status"] == "analyzing"
    )
    active_trials = counts["active_trials"] > 0 or counts["trial_jobs_active"]
    cancel = (
        "task"
        if active_trials
        else "qa"
        if qa_active or analysis_active
        else "task"
        if counts["jobs_active"]
        else None
    )
    task = TaskStatusResponse(
        **row,
        **counts,
        experiment_id="",
        experiment_name="",
        progress="",
        started_at=None,
        finished_at=None,
    )
    selected = (
        None
        if row["version_id"] is None
        else TaskVersionSummary(
            id=row["version_id"],
            version=row["version"],
            content_hash=row["content_hash"],
            created_at=row["version_created_at"],
            is_current=row["version_id"] == row["current_version_id"],
            pre_trial_findings=(row["pre_trial"] or {}).get("items") or [],
            pre_trial_status=row["pre_trial_status"],
            pre_trial_error=row["pre_trial_error"],
        )
    )
    return TaskPanelResponse(
        task=task,
        version=selected,
        can_retry=counts["can_retry"],
        cancel=cancel,
        active_trials=counts["active_trials"],
        qa_active=qa_active,
        can_run_qa=(
            counts["total"] > 0
            and counts["total"]
            == counts["completed"] + counts["failed"] + counts["skipped"]
            and not analysis_active
            and not qa_active
        ),
        has_analysis=counts["has_analysis"],
    )

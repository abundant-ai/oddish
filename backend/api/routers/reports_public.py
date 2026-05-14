"""Anonymous (no-auth) read endpoints for shared reports.

Viewers see name + description + created_at + creator-display + the
rendered grid + trial detail deep-links. The YAML spec, audit history,
source experiment ids, and org-internal task metadata (github_meta,
task_path, worker jobs, verdict errors) are deliberately *not*
exposed — public viewers see what was compared, not how it was
authored.

There is no public index — discovery is by link only.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select

from models import UserModel
from oddish.core.public_helpers import (
    get_task_file_content_s3,
    get_trial_file_content_s3,
    list_task_files_s3,
    list_trial_files_s3,
)
from oddish.core.reports import (
    build_public_report_response,
    display_for_user,
    get_public_report,
    resolve_report_cells,
    trial_id_set_for_public_access,
)
from oddish.core.trial_io import (
    read_trial_agent_file,
    read_trial_logs,
    read_trial_logs_structured,
    read_trial_result,
    read_trial_trajectory,
)
from oddish.db import (
    TaskVersionModel,
    TrialModel,
    get_session,
)
from oddish.schemas import PublicReportResponse


router = APIRouter(tags=["Public Reports"])


async def _load_trial_for_public_report(
    token: str, trial_id: str
) -> TrialModel:
    """Resolve `(token, trial_id)` to a detached TrialModel.

    Enforces the "trial must appear in this report's resolved cells"
    rule. The trial is expunged from the session so the caller can do
    artifact I/O without holding an open transaction.
    """
    async with get_session() as session:
        report = await get_public_report(session, token)
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        _rows, _cols, cells = await resolve_report_cells(session, report)
        allowed = trial_id_set_for_public_access(cells)
        if trial_id not in allowed:
            raise HTTPException(status_code=404, detail="Trial not found")

        trial = await session.get(TrialModel, trial_id)
        if not trial:
            raise HTTPException(status_code=404, detail="Trial not found")
        session.expunge(trial)
        return trial


# ---------------------------------------------------------------------------
# Main public endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/public/reports/{public_token}", response_model=PublicReportResponse
)
async def get_public_report_view(public_token: str) -> PublicReportResponse:
    async with get_session() as session:
        report = await get_public_report(session, public_token)
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        rows, columns, cells = await resolve_report_cells(session, report)

        created_by_display: str | None = None
        if report.created_by_user_id:
            user = await session.get(UserModel, report.created_by_user_id)
            if user:
                created_by_display = display_for_user(
                    email=user.email, name=user.name
                )

        return build_public_report_response(
            report,
            rows,
            columns,
            cells,
            created_by_display=created_by_display,
        )


# ---------------------------------------------------------------------------
# Trial deep links — only valid for trials present in the resolved cells
# ---------------------------------------------------------------------------


@router.get("/public/reports/{public_token}/trials/{trial_id}/logs")
async def get_public_report_trial_logs(
    public_token: str, trial_id: str
) -> dict:
    trial = await _load_trial_for_public_report(public_token, trial_id)
    return await read_trial_logs(trial)


@router.get("/public/reports/{public_token}/trials/{trial_id}/logs/structured")
async def get_public_report_trial_logs_structured(
    public_token: str, trial_id: str
) -> dict:
    trial = await _load_trial_for_public_report(public_token, trial_id)
    return await read_trial_logs_structured(trial)


@router.get("/public/reports/{public_token}/trials/{trial_id}/trajectory")
async def get_public_report_trial_trajectory(
    public_token: str, trial_id: str
) -> dict | None:
    trial = await _load_trial_for_public_report(public_token, trial_id)
    return await read_trial_trajectory(trial)


@router.get("/public/reports/{public_token}/trials/{trial_id}/result")
async def get_public_report_trial_result(
    public_token: str, trial_id: str
) -> dict:
    trial = await _load_trial_for_public_report(public_token, trial_id)
    return await read_trial_result(trial)


@router.get("/public/reports/{public_token}/trials/{trial_id}/files")
async def list_public_report_trial_files(
    public_token: str,
    trial_id: str,
    prefix: str | None = Query(None),
    recursive: bool = Query(True),
    limit: int = Query(1000, ge=1, le=1000),
    cursor: str | None = Query(None),
    presign: bool = Query(True),
) -> dict:
    trial = await _load_trial_for_public_report(public_token, trial_id)
    return await list_trial_files_s3(
        trial,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
    )


@router.get(
    "/public/reports/{public_token}/trials/{trial_id}/files/{file_path:path}"
)
async def get_public_report_trial_file(
    public_token: str, trial_id: str, file_path: str
) -> Response:
    trial = await _load_trial_for_public_report(public_token, trial_id)
    try:
        content, media_type = await get_trial_file_content_s3(trial, file_path)
        return Response(content=content, media_type=media_type)
    except HTTPException:
        pass
    content, media_type = await read_trial_agent_file(trial, file_path)
    return Response(content=content, media_type=media_type)


# ---------------------------------------------------------------------------
# Task file deep links — same access rule (trial in the resolved cells
# implies the task version is in the report).
# ---------------------------------------------------------------------------


async def _resolve_task_version_for_report(
    token: str, task_id: str, version: int | None
) -> int | None:
    async with get_session() as session:
        report = await get_public_report(session, token)
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # The report references task versions explicitly; reject task
        # ids that aren't in the row set so anon viewers can't probe
        # arbitrary tasks via a known report token.
        allowed_tv_ids = {tv.task_version_id for tv in report.task_versions}
        if not allowed_tv_ids:
            raise HTTPException(status_code=404, detail="Task not found")
        tv_rows = await session.execute(
            select(TaskVersionModel).where(
                TaskVersionModel.id.in_(allowed_tv_ids),
                TaskVersionModel.task_id == task_id,
            )
        )
        tvs = list(tv_rows.scalars().all())
        if not tvs:
            raise HTTPException(status_code=404, detail="Task not found")

        if version is None:
            # Pick the newest version that the report actually references.
            tvs.sort(key=lambda t: t.version, reverse=True)
            return tvs[0].version

        match = next((t for t in tvs if t.version == version), None)
        if not match:
            raise HTTPException(status_code=404, detail="Task version not found")
        return match.version


@router.get("/public/reports/{public_token}/tasks/{task_id}/files")
async def list_public_report_task_files(
    public_token: str,
    task_id: str,
    prefix: str | None = Query(None),
    recursive: bool = Query(True),
    limit: int = Query(1000, ge=1, le=1000),
    cursor: str | None = Query(None),
    presign: bool = Query(True),
    version: int | None = Query(None, description="Task version number"),
) -> dict:
    resolved = await _resolve_task_version_for_report(
        public_token, task_id, version
    )
    return await list_task_files_s3(
        task_id=task_id,
        prefix=prefix,
        recursive=recursive,
        limit=limit,
        cursor=cursor,
        presign=presign,
        version=resolved,
    )


@router.get(
    "/public/reports/{public_token}/tasks/{task_id}/files/{file_path:path}"
)
async def get_public_report_task_file(
    public_token: str,
    task_id: str,
    file_path: str,
    presign: bool = Query(False),
    version: int | None = Query(None, description="Task version number"),
) -> dict:
    resolved = await _resolve_task_version_for_report(
        public_token, task_id, version
    )
    return await get_task_file_content_s3(
        task_id=task_id,
        file_path=file_path,
        presign=presign,
        version=resolved,
    )

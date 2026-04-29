"""Drag-and-drop zip import endpoints (UI alternative to ``oddish upload``)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from oddish.core.zip_imports import (
    import_zip,
    inspect_zip,
    summarize_response,
)
from auth import APIKeyScope, AuthContext, require_auth
from oddish.db import Priority

router = APIRouter(tags=["Imports"])


# Cap at 1 GiB per uploaded file. Harbor job dirs with full agent
# trajectories can run hundreds of MB; anything beyond a gig is almost
# certainly a misclick (full datasets, video captures, etc.) and the
# CLI is the right tool for that case.
_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


async def _stash_upload(upload: UploadFile, dest: Path) -> int:
    """Stream *upload* to disk, returning the byte count written."""
    total = 0
    with dest.open("wb") as fh:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Upload exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} "
                        "MiB per-file limit. Use the `oddish upload` CLI for "
                        "larger archives."
                    ),
                )
            fh.write(chunk)
    return total


@router.post("/imports/zip")
async def import_zip_endpoint(
    auth: Annotated[AuthContext, Depends(require_auth)],
    task_zip: Annotated[UploadFile | None, File()] = None,
    run_zip: Annotated[UploadFile | None, File()] = None,
    task_id: Annotated[str | None, Form()] = None,
    experiment: Annotated[str | None, Form()] = None,
    message: Annotated[str | None, Form()] = None,
    skip_artifacts: Annotated[bool, Form()] = False,
    priority: Annotated[str | None, Form()] = None,
) -> dict:
    """Import a Harbor task and/or run zip in one shot.

    Mirrors ``oddish upload`` end-to-end:

    - ``task_zip`` only -> register/update a task version.
    - ``run_zip`` only -> import every Harbor trial inside it onto the
      ``task_id`` provided in the form. ``task_id`` accepts a task
      name as well; if blank, the task name is inferred from the run's
      Harbor metadata + zip filename.
    - both -> upload the task first, then import the trials against the
      newly-created task (CLI ``--path`` flow).
    """
    auth.require_scope(APIKeyScope.TASKS)

    if task_zip is None and run_zip is None:
        raise HTTPException(
            status_code=400, detail="Provide a task zip, a run zip, or both."
        )

    workspace = Path(tempfile.mkdtemp(prefix="oddish-zip-upload-"))
    try:
        task_zip_path: Path | None = None
        if task_zip is not None:
            task_zip_path = workspace / "task.zip"
            await _stash_upload(task_zip, task_zip_path)

        run_zip_path: Path | None = None
        run_zip_filename: str | None = None
        if run_zip is not None:
            run_zip_path = workspace / "run.zip"
            await _stash_upload(run_zip, run_zip_path)
            # Browsers send the filename even for streamed uploads; we
            # use it as a last-resort task-name hint when the run's
            # JSON doesn't have a task_path stamped in.
            run_zip_filename = run_zip.filename

        priority_enum: Priority | None = None
        if priority:
            try:
                priority_enum = Priority(priority.lower())
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid priority: {priority!r}",
                ) from exc

        result = await import_zip(
            task_zip_path=task_zip_path,
            run_zip_path=run_zip_path,
            run_zip_filename=run_zip_filename,
            target_task_id=(task_id or None),
            experiment_id_or_name=(experiment or None),
            upload_artifacts=not skip_artifacts,
            message=message,
            org_id=auth.org_id,
            user_id=auth.user_id,
            user_name=None,
            priority=priority_enum,
        )

        return summarize_response(result)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


@router.post("/imports/zip/inspect")
async def inspect_zip_endpoint(
    auth: Annotated[AuthContext, Depends(require_auth)],
    zip_file: Annotated[UploadFile, File()],
) -> dict:
    """Peek into a zip and report what it contains, without importing.

    Lets the import dialog show "12 trials in 3 jobs" or "task: my-task"
    before the user commits to the upload. Read-only -- never touches
    the database or S3.
    """
    auth.require_scope(APIKeyScope.READ)

    workspace = Path(tempfile.mkdtemp(prefix="oddish-zip-inspect-"))
    try:
        zip_path = workspace / "upload.zip"
        await _stash_upload(zip_file, zip_path)
        inspection = inspect_zip(zip_path, filename=zip_file.filename)
        return {
            "is_task": inspection.is_task,
            "is_job": inspection.is_job,
            "is_jobs": inspection.is_jobs,
            "trial_count": inspection.trial_count,
            "job_count": inspection.job_count,
            "task_name": inspection.task_name,
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

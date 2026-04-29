"""Server-side handler for the drag-and-drop zip import flow.

Mirrors the ``oddish upload`` CLI:

- A task zip (a Harbor task directory packed as ``.zip``) becomes a new
  task version (or a no-op when the content hash matches the latest
  version).
- A run zip (a Harbor ``job_dir`` or parent ``jobs_dir`` packed as
  ``.zip``) becomes one or more imported trial rows under a target task
  and experiment.
- Both can be uploaded together to do "first time importing a brand-new
  task" in one step (the equivalent of ``oddish upload ./jobs --path
  ./my-task``).

The actual API surface lives in ``backend.api.routers.imports``; this
module is the framework-agnostic core so the CLI and a future
self-hosted bundle can share it.

Harbor run dirs do not embed the task source -- ``harbor view`` runs
on top of result.json/config.json/trajectory files alone -- so the
import flow has to extract the task *name* from the run and look up an
existing oddish task by that name. We chain three sources (job-dir
naming convention, JSON config/result blobs, zip filename) so common
shapes Just Work without the user having to type anything.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import and_, select

from oddish.cli.api import (
    _tar_trial_dir,
    archive_task_dir,
    compute_task_content_hash,
    detect_trajectory_in_dir,
    discover_trial_entries,
    is_harbor_job_dir,
    is_harbor_jobs_dir,
    is_task_dir,
    load_harbor_trial_result,
    trial_result_to_import_spec,
)
from oddish.core.tasks import complete_task_upload, initialize_task_upload
from oddish.core.trial_imports import (
    complete_trial_import,
    initialize_trial_import,
)
from oddish.db import Priority, TaskModel, get_session
from oddish.experiment import generate_experiment_name
from oddish.schemas import ImportedTrialSpec


# Concurrency for the per-trial fan-out. Same default as the CLI's
# ``TRIAL_IMPORT_CONCURRENCY`` so server-side imports behave the same
# as a local ``oddish upload`` for sizing.
_TRIAL_IMPORT_CONCURRENCY = 4


# =============================================================================
# Result dataclasses
# =============================================================================


@dataclass
class ZipImportTaskResult:
    task_id: str
    name: str
    version: int | None
    existing_task: bool
    content_unchanged: bool


@dataclass
class ZipImportTrialResult:
    job_name: str
    trial_name: str
    trial_id: str | None
    status: str  # "imported" | "error"
    error: str | None = None
    files_extracted: int = 0


@dataclass
class ZipImportResult:
    task: ZipImportTaskResult | None = None
    experiment_id: str | None = None
    experiment_name: str | None = None
    trials: list[ZipImportTrialResult] = field(default_factory=list)


# =============================================================================
# Zip helpers
# =============================================================================


def extract_zip_to_dir(zip_path: Path, dest_dir: Path) -> Path:
    """Extract *zip_path* into *dest_dir* and return the meaningful root.

    When the zip contains a single top-level directory (the common case
    when a user runs ``zip -r my-task.zip my-task``) we descend into it
    so the caller doesn't have to special-case both layouts.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                # Defense-in-depth against zip-slip: reject absolute paths
                # and any segment that traverses upward.
                name = member.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Refusing to extract unsafe zip entry: {name}",
                    )
            zf.extractall(dest_dir)
    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=400, detail=f"Uploaded file is not a valid zip: {exc}"
        ) from exc

    entries = [p for p in dest_dir.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest_dir


# =============================================================================
# Task-name inference (no task files needed)
# =============================================================================


def _harbor_task_name_from_job_dir(job_dir_name: str) -> str | None:
    """Extract the task name from a Harbor job dir like ``<task>.<agent>.<id>``.

    Harbor's convention is ``<task_name>.<agent>.<short_id>``; the task
    name itself can contain dots and dashes, so we slice off the last
    two ``.``-separated segments rather than splitting from the left.
    Returns None when the dir name has fewer than two dots (and so
    can't possibly match the convention).
    """
    parts = job_dir_name.rsplit(".", 2)
    if len(parts) < 3:
        return None
    return parts[0] or None


def _walk_for_task_name(obj: Any, depth: int = 0) -> str | None:
    """Best-effort task-name extraction from a Harbor JSON blob.

    Harbor stamps ``task_path`` on every JobConfig (the path the user
    pointed ``harbor run`` at); the basename of that path is the task
    name. Other shapes (``task.name``, ``task_name``) appear in
    different harbor versions, so we accept those too. Recurses through
    nested dicts/lists with a small depth cap.
    """
    if depth > 6:
        return None
    if not isinstance(obj, dict):
        return None

    for key in ("task_name", "taskName"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value

    path_value = obj.get("task_path")
    if isinstance(path_value, str):
        tail = Path(path_value).name
        if tail:
            return tail

    task = obj.get("task")
    if isinstance(task, dict):
        for key in ("name", "task_name"):
            value = task.get(key)
            if isinstance(value, str) and value:
                return value
        for key in ("path", "task_path"):
            value = task.get(key)
            if isinstance(value, str):
                tail = Path(value).name
                if tail:
                    return tail

    for child in obj.values():
        if isinstance(child, dict):
            found = _walk_for_task_name(child, depth + 1)
            if found:
                return found
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    found = _walk_for_task_name(item, depth + 1)
                    if found:
                        return found
    return None


def _read_json_safely(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _task_name_from_run_artifacts(run_root: Path) -> str | None:
    """Read result/config JSON files inside the run for an embedded task name.

    Order: job-level JSON first (one per run), then trial-level JSON
    (one per trial). Job-level config is the canonical place Harbor
    stores ``task_path``; trials are a backup for cases where the
    job-level file was excluded from the zip.
    """
    candidates: list[Path] = []
    for fname in ("config.json", "result.json"):
        candidate = run_root / fname
        if candidate.is_file():
            candidates.append(candidate)

    try:
        children = list(run_root.iterdir())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir():
            continue
        for fname in ("config.json", "result.json"):
            candidate = child / fname
            if candidate.is_file():
                candidates.append(candidate)

    for path in candidates:
        data = _read_json_safely(path)
        name = _walk_for_task_name(data)
        if name:
            return name
    return None


def _task_name_from_filename(filename: str | None) -> str | None:
    """Strip a browser-uploaded zip filename to its task-name stem.

    ``milestone-based-task (3).zip`` -> ``milestone-based-task``.
    Browsers add `` (N)`` for repeat-name downloads; that suffix is
    stripped. Single-name zips with no special chars roundtrip cleanly.
    """
    if not filename:
        return None
    stem = filename
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem).strip()
    return stem or None


def _infer_task_name_candidates(
    run_root: Path, *, run_zip_filename: str | None
) -> list[str]:
    """Return distinct task-name candidates ordered by source confidence."""
    sources: list[str | None] = []

    if is_harbor_job_dir(run_root):
        sources.append(_harbor_task_name_from_job_dir(run_root.name))
    elif is_harbor_jobs_dir(run_root):
        # Look at the child job dirs' names; pick a common prefix
        # only when every job dir agrees on it.
        try:
            child_candidates = {
                _harbor_task_name_from_job_dir(child.name)
                for child in run_root.iterdir()
                if child.is_dir() and is_harbor_job_dir(child)
            }
        except OSError:
            child_candidates = set()
        child_candidates.discard(None)
        if len(child_candidates) == 1:
            sources.append(child_candidates.pop())

    sources.append(_task_name_from_run_artifacts(run_root))
    sources.append(_task_name_from_filename(run_zip_filename))

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in sources:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


@dataclass
class ZipInspection:
    is_task: bool
    is_job: bool
    is_jobs: bool
    trial_count: int
    job_count: int
    task_name: str | None


def inspect_extracted(
    root: Path, *, run_zip_filename: str | None = None
) -> ZipInspection:
    """Classify *root* (the extracted zip contents) into one of the four shapes."""
    is_task = is_task_dir(root)
    is_job = is_harbor_job_dir(root)
    is_jobs = is_harbor_jobs_dir(root)

    trial_count = 0
    job_count = 0
    task_name: str | None = None

    if is_task:
        task_name = root.name
    elif is_job or is_jobs:
        entries = discover_trial_entries(root)
        trial_count = len(entries)
        if is_jobs:
            job_count = len({entry[0] for entry in entries})
        else:
            job_count = 1
        candidates = _infer_task_name_candidates(
            root, run_zip_filename=run_zip_filename
        )
        if candidates:
            task_name = candidates[0]

    return ZipInspection(
        is_task=is_task,
        is_job=is_job,
        is_jobs=is_jobs,
        trial_count=trial_count,
        job_count=job_count,
        task_name=task_name,
    )


# =============================================================================
# Task upload (server-side equivalent of ``oddish.cli.api.upload_task``)
# =============================================================================


async def _put_to_presigned(
    url: str, archive_path: Path, headers: dict[str, str]
) -> None:
    """PUT a local archive to a presigned URL.

    The CLI does this client-side; when the request originates in the
    browser we still want the same single-upload-per-archive contract,
    so the server stages the archive locally and pushes it to S3 via
    the presigned URL it just minted. Reuses the existing storage path
    end-to-end (``initialize_*`` mints, ``complete_*`` extracts).
    """
    upload_headers = dict(headers)
    upload_headers.setdefault("Content-Length", str(archive_path.stat().st_size))
    async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
        response = await client.put(
            url,
            headers=upload_headers,
            content=archive_path.read_bytes(),
        )
    if response.status_code not in {200, 201, 204}:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Storage upload failed with {response.status_code}: "
                f"{response.text[:200]}"
            ),
        )


async def _upload_task_dir(
    task_dir: Path,
    *,
    org_id: str | None,
    user_id: str | None,
    user_name: str | None,
    priority: Priority | None,
    message: str | None,
) -> ZipImportTaskResult:
    content_hash = compute_task_content_hash(task_dir)
    init = await initialize_task_upload(
        task_dir.name,
        org_id=org_id,
        content_hash=content_hash,
        message=message,
    )

    # Server returns the existing version row when the content hash
    # matches; nothing more to do in that case.
    if init.content_unchanged:
        return ZipImportTaskResult(
            task_id=init.task_id,
            name=init.name,
            version=init.version,
            existing_task=True,
            content_unchanged=True,
        )

    if not init.upload_url or init.version is None:
        raise HTTPException(
            status_code=500,
            detail="Task upload init did not return a presigned URL.",
        )

    archive_path = archive_task_dir(task_dir)
    try:
        await _put_to_presigned(
            init.upload_url, archive_path, init.upload_headers or {}
        )
    finally:
        shutil.rmtree(archive_path.parent, ignore_errors=True)

    complete = await complete_task_upload(
        task_id=init.task_id,
        task_name=init.name,
        version=init.version,
        content_hash=content_hash,
        message=message,
        org_id=org_id,
        created_by_user_id=user_id,
        register=True,
        user=user_name,
        priority=priority,
    )

    return ZipImportTaskResult(
        task_id=complete.task_id,
        name=complete.name,
        version=complete.version,
        existing_task=bool(complete.existing_task),
        content_unchanged=False,
    )


# =============================================================================
# Target task resolution (ID or name)
# =============================================================================


async def _resolve_task_id_or_name(
    identifier: str, org_id: str | None
) -> str | None:
    """Resolve a target task identifier to its canonical ``task_id``.

    Tries the literal as a task ID first (cheap PK lookup), falls back
    to a ``(org_id, name)`` match. Returns None when neither hits.

    Lets the import dialog accept either form: the task name is what
    users see in the tasks browser, while task IDs are the suffixed
    form (e.g. ``mytask-abc12345``) that the CLI deals in.
    """
    async with get_session() as session:
        by_id = await session.get(TaskModel, identifier)
        if by_id is not None and (org_id is None or by_id.org_id == org_id):
            return by_id.id

        if org_id is None:
            clause = and_(TaskModel.name == identifier, TaskModel.org_id.is_(None))
        else:
            clause = and_(TaskModel.name == identifier, TaskModel.org_id == org_id)
        by_name = await session.scalar(select(TaskModel).where(clause))
        if by_name is not None:
            return by_name.id

    return None


# =============================================================================
# Trial import (server-side equivalent of ``oddish.cli.api.import_trial``)
# =============================================================================


async def _import_one_trial(
    *,
    task_id: str,
    experiment_id: str | None,
    job_name: str,
    trial_name: str,
    trial_dir: Path,
    upload_artifacts: bool,
    org_id: str | None,
) -> tuple[ZipImportTrialResult, str | None, str | None]:
    """Import one trial. Returns (result, experiment_id, experiment_name)."""
    trial_result = load_harbor_trial_result(trial_dir)
    if trial_result is None:
        return (
            ZipImportTrialResult(
                job_name=job_name,
                trial_name=trial_name,
                trial_id=None,
                status="error",
                error="Could not load Harbor result.json from trial dir",
            ),
            None,
            None,
        )

    has_trajectory = detect_trajectory_in_dir(trial_dir)
    spec_payload = trial_result_to_import_spec(
        trial_result, has_trajectory=has_trajectory
    )
    trial_spec = ImportedTrialSpec.model_validate(spec_payload)

    init = await initialize_trial_import(
        task_id=task_id,
        experiment_id_or_name=experiment_id,
        trial_spec=trial_spec,
        upload_artifacts=upload_artifacts,
        org_id=org_id,
    )

    files_extracted = 0
    if upload_artifacts and init.upload_url:
        archive_path = _tar_trial_dir(trial_dir)
        try:
            await _put_to_presigned(
                init.upload_url, archive_path, init.upload_headers or {}
            )
        finally:
            shutil.rmtree(archive_path.parent, ignore_errors=True)
        complete = await complete_trial_import(trial_id=init.trial_id, org_id=org_id)
        files_extracted = complete.files_extracted

    return (
        ZipImportTrialResult(
            job_name=job_name,
            trial_name=trial_name,
            trial_id=init.trial_id,
            status="imported",
            files_extracted=files_extracted,
        ),
        init.experiment_id,
        init.experiment_name,
    )


async def _import_trials(
    *,
    job_root: Path,
    task_id: str,
    experiment_id_or_name: str | None,
    upload_artifacts: bool,
    org_id: str | None,
) -> tuple[list[ZipImportTrialResult], str | None, str | None]:
    """Import every trial under *job_root* into *task_id*.

    The first trial is imported sequentially so the auto-named
    experiment row is created exactly once before the fan-out -- same
    rationale as the CLI (``oddish.cli.upload._run_trial_import``).
    """
    entries = discover_trial_entries(job_root)
    if not entries:
        raise HTTPException(
            status_code=400,
            detail=(
                "No Harbor trials found in the run zip. Expected a job dir "
                "with per-trial subdirs, each containing a result.json."
            ),
        )

    effective_experiment = experiment_id_or_name or generate_experiment_name()

    results: list[ZipImportTrialResult] = []
    experiment_id: str | None = None
    experiment_name: str | None = None

    first_job, first_name, first_dir = entries[0]
    first_result, experiment_id, experiment_name = await _import_one_trial(
        task_id=task_id,
        experiment_id=effective_experiment,
        job_name=first_job,
        trial_name=first_name,
        trial_dir=first_dir,
        upload_artifacts=upload_artifacts,
        org_id=org_id,
    )
    results.append(first_result)

    remaining = entries[1:]
    if remaining:
        sem = asyncio.Semaphore(_TRIAL_IMPORT_CONCURRENCY)

        async def _bounded(
            entry: tuple[str, str, Path],
        ) -> tuple[ZipImportTrialResult, str | None, str | None]:
            async with sem:
                job_name, trial_name, trial_dir = entry
                return await _import_one_trial(
                    task_id=task_id,
                    experiment_id=effective_experiment,
                    job_name=job_name,
                    trial_name=trial_name,
                    trial_dir=trial_dir,
                    upload_artifacts=upload_artifacts,
                    org_id=org_id,
                )

        for tup in await asyncio.gather(*(_bounded(e) for e in remaining)):
            res, exp_id, exp_name = tup
            results.append(res)
            # Capture experiment metadata if the first trial errored but
            # a later one succeeded.
            if experiment_id is None and exp_id is not None:
                experiment_id = exp_id
                experiment_name = exp_name

    return results, experiment_id, experiment_name


# =============================================================================
# Top-level entry point
# =============================================================================


async def import_zip(
    *,
    task_zip_path: Path | None,
    run_zip_path: Path | None,
    run_zip_filename: str | None = None,
    target_task_id: str | None,
    experiment_id_or_name: str | None,
    upload_artifacts: bool,
    message: str | None,
    org_id: str | None,
    user_id: str | None,
    user_name: str | None,
    priority: Priority | None,
) -> ZipImportResult:
    """Drive a full drag-and-drop import end to end.

    Resolution rules (mirror ``oddish upload`` semantics):

    - ``task_zip`` alone -> task upload only.
    - ``run_zip`` alone -> trial import. ``target_task_id`` accepts
      either a task ID or a task name within the caller's org. When
      blank, the task name is inferred from the run (job-dir
      convention, then config.json/result.json contents, then the zip
      filename).
    - ``task_zip`` + ``run_zip`` -> upload the task first, then import
      the trials against it (CLI ``--path`` flow).

    Harbor runs do not bundle the task source, which is why all three
    inference sources are tried before giving up: a user can drop just
    the run zip and have it land on the existing oddish task without
    having to type or look up an ID.
    """
    if task_zip_path is None and run_zip_path is None:
        raise HTTPException(
            status_code=400,
            detail="Provide a task zip, a run zip, or both.",
        )

    workspace = Path(tempfile.mkdtemp(prefix="oddish-zip-import-"))
    try:
        task_result: ZipImportTaskResult | None = None
        resolved_task_id: str | None = None

        if task_zip_path is not None:
            task_extract_dir = workspace / "task"
            task_root = extract_zip_to_dir(task_zip_path, task_extract_dir)
            if not is_task_dir(task_root):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Task zip does not look like a Harbor task directory "
                        "(missing task.toml / instruction.md / environment / tests)."
                    ),
                )
            task_result = await _upload_task_dir(
                task_root,
                org_id=org_id,
                user_id=user_id,
                user_name=user_name,
                priority=priority,
                message=message,
            )
            resolved_task_id = task_result.task_id
        elif target_task_id:
            resolved_task_id = await _resolve_task_id_or_name(
                target_task_id, org_id
            )
            if resolved_task_id is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No task matching {target_task_id!r} (by ID or "
                        "name) in this org. Either drop the task files "
                        "alongside the run zip, or pick an existing task."
                    ),
                )

        trial_results: list[ZipImportTrialResult] = []
        experiment_id: str | None = None
        experiment_name: str | None = None

        if run_zip_path is not None:
            run_extract_dir = workspace / "run"
            run_root = extract_zip_to_dir(run_zip_path, run_extract_dir)
            if not (is_harbor_job_dir(run_root) or is_harbor_jobs_dir(run_root)):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Run zip does not look like a Harbor job dir. Expected "
                        "a directory containing result.json (single job) or a "
                        "parent directory of such job subdirs."
                    ),
                )

            # Run-only flow: try every plausible task-name source in
            # order until one matches an oddish task. Harbor runs have
            # no embedded task source but they do stamp the task
            # name/path into config.json, so we don't normally need
            # the user to type anything.
            if resolved_task_id is None:
                candidates = _infer_task_name_candidates(
                    run_root, run_zip_filename=run_zip_filename
                )
                for candidate in candidates:
                    match = await _resolve_task_id_or_name(candidate, org_id)
                    if match is not None:
                        resolved_task_id = match
                        break

                if resolved_task_id is None:
                    if candidates:
                        candidates_str = ", ".join(repr(c) for c in candidates)
                        detail = (
                            "Importing trials requires a target task. Tried "
                            f"these names from the run zip: {candidates_str} "
                            "-- none matched a task in this org. Either "
                            "type the existing task's ID or name in the "
                            "field, or expand 'Upload task files too' and "
                            "drop the task zip alongside the run."
                        )
                    else:
                        detail = (
                            "Importing trials requires a target task and we "
                            "couldn't infer one from the run zip. Type the "
                            "task ID/name in the field, or upload the task "
                            "files alongside the run."
                        )
                    raise HTTPException(status_code=404, detail=detail)

            trial_results, experiment_id, experiment_name = await _import_trials(
                job_root=run_root,
                task_id=resolved_task_id,
                experiment_id_or_name=experiment_id_or_name,
                upload_artifacts=upload_artifacts,
                org_id=org_id,
            )

        return ZipImportResult(
            task=task_result,
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            trials=trial_results,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# =============================================================================
# Inspection-only entry point (for the dialog's preview before commit)
# =============================================================================


def inspect_zip(zip_path: Path, *, filename: str | None = None) -> ZipInspection:
    """Peek into *zip_path* without touching the database or S3."""
    workspace = Path(tempfile.mkdtemp(prefix="oddish-zip-inspect-"))
    try:
        root = extract_zip_to_dir(zip_path, workspace)
        return inspect_extracted(root, run_zip_filename=filename)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def summarize_response(result: ZipImportResult) -> dict[str, Any]:
    """Serialize a :class:`ZipImportResult` as the JSON payload returned by the API."""
    return {
        "task": (
            {
                "task_id": result.task.task_id,
                "name": result.task.name,
                "version": result.task.version,
                "existing_task": result.task.existing_task,
                "content_unchanged": result.task.content_unchanged,
            }
            if result.task is not None
            else None
        ),
        "experiment_id": result.experiment_id,
        "experiment_name": result.experiment_name,
        "trials": [
            {
                "job_name": t.job_name,
                "trial_name": t.trial_name,
                "trial_id": t.trial_id,
                "status": t.status,
                "error": t.error,
                "files_extracted": t.files_extracted,
            }
            for t in result.trials
        ],
        "trial_count": len(result.trials),
        "trials_imported": sum(1 for t in result.trials if t.status == "imported"),
        "trials_failed": sum(1 for t in result.trials if t.status == "error"),
    }

"""``TASK_EXPAND`` worker-job handler.

Expands a task's ``.oddish-task.tar.gz`` archive into a sibling per-file
S3 layout at ``tasks/{task_id}/v{N}-files/`` plus a
``.oddish-manifest.json`` sentinel that records the source archive's
etag. The canonical archive is never modified — this is a derived cache
that lets the task-files drawer list objects directly and fetch content
via per-file presigned URLs, matching the fast path trial files already
use.

Payload shape::

    {"task_id": str, "version": int}

The handler is idempotent: if a manifest already exists and its
``archive_etag`` matches the current archive, expansion short-circuits,
``expanded_at`` is refreshed, and no objects are re-uploaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import mimetypes
import tarfile
from datetime import datetime, timezone

from sqlalchemy import select

from oddish.config import settings
from oddish.db import (
    TaskVersionModel,
    get_session,
    get_storage_client,
    utcnow,
)
from oddish.db.storage import StorageClient, normalize_s3_relative_path
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

TASK_EXPAND_TIMEOUT = 600  # 10 minutes
TASK_EXPAND_HEARTBEAT_INTERVAL_SECONDS = 30

_MAX_CONCURRENT_MEMBER_UPLOADS = 8


async def _heartbeat_task_expand_worker_job(
    *,
    worker_job_id: str,
    stop_event: asyncio.Event,
) -> None:
    """Keep ``worker_jobs.heartbeat_at`` fresh during a slow expansion."""
    consecutive_failures = 0
    pending_failure_count = 0
    pending_last_error: str | None = None

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=TASK_EXPAND_HEARTBEAT_INTERVAL_SECONDS
            )
        except TimeoutError:
            pass

        if stop_event.is_set():
            return

        try:
            await heartbeat_worker_job(
                worker_job_id,
                pending_failure_count=pending_failure_count,
                pending_last_error=pending_last_error,
            )
            if consecutive_failures > 0:
                console.print(
                    f"[green]TASK_EXPAND worker_job {worker_job_id} heartbeat "
                    f"recovered after {consecutive_failures} failure(s)[/green]"
                )
            consecutive_failures = 0
            pending_failure_count = 0
            pending_last_error = None
        except Exception as exc:
            consecutive_failures += 1
            pending_failure_count += 1
            pending_last_error = f"{type(exc).__name__}: {exc}"


def _expanded_prefix_for(task_id: str, version: int) -> str:
    """Sibling-prefix layout (``v{N}-files/``) keeps expansion artifacts
    from leaking into the archive branch's non-archive listing path."""
    return f"tasks/{task_id}/v{version}-files/"


def _task_archive_key_for(task_id: str, version: int) -> str:
    return (
        f"tasks/{task_id}/v{version}/{StorageClient._TASK_ARCHIVE_OBJECT_NAME}"
    )


async def _maybe_short_circuit_on_manifest(
    storage: StorageClient,
    *,
    manifest_key: str,
    archive_etag: str | None,
) -> dict | None:
    """Return the stored manifest when it's already in sync with the archive."""
    if not await storage.object_exists(manifest_key):
        return None
    try:
        manifest = await storage.download_json(manifest_key)
    except Exception:
        return None
    stored_etag = manifest.get("archive_etag")
    if archive_etag is not None and stored_etag == archive_etag:
        return manifest
    return None


def _parse_members(archive_bytes: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        return [m for m in tar.getmembers() if m.isfile()]


def _read_member_bytes(archive_bytes: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        member = tar.getmember(name)
        extracted = tar.extractfile(member)
        if extracted is None:
            return b""
        return extracted.read()


async def run_task_expand_job(
    task_id: str,
    version: int,
    *,
    worker_job_id: str | None = None,
) -> dict:
    """Expand a task version's tarball into the per-file layout.

    Returns a small result summary suitable for writing back to
    ``worker_jobs.result_summary`` (file count, skip reason, etc). Raises
    on unrecoverable errors so the runner's outcome pipeline records a
    retryable failure.
    """
    console.print(
        f"[cyan]Processing TASK_EXPAND[/cyan] task_id={task_id} version={version}"
    )

    storage = get_storage_client()
    archive_key = _task_archive_key_for(task_id, version)
    expanded_prefix = _expanded_prefix_for(task_id, version)
    manifest_key = f"{expanded_prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"

    heartbeat_stop = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    if worker_job_id:
        heartbeat_task = asyncio.create_task(
            _heartbeat_task_expand_worker_job(
                worker_job_id=worker_job_id,
                stop_event=heartbeat_stop,
            )
        )

    try:
        # Confirm the source archive exists and grab its size + etag.
        await storage._ensure_client()  # type: ignore[attr-defined]
        try:
            head = await storage._s3.head_object(  # type: ignore[attr-defined]
                Bucket=settings.s3_bucket, Key=archive_key
            )
        except Exception as exc:
            raise RuntimeError(
                f"Task archive missing at {archive_key}: {exc}"
            ) from exc

        archive_size = int(head.get("ContentLength") or 0)
        raw_etag = head.get("ETag")
        archive_etag = str(raw_etag) if raw_etag else None

        max_bytes = int(settings.tasks_expand_max_bytes)
        if max_bytes and archive_size > max_bytes:
            summary = {
                "status": "skipped",
                "reason": "archive_too_large",
                "archive_size": archive_size,
                "limit": max_bytes,
            }
            console.print(
                f"[yellow]TASK_EXPAND skip: archive_size={archive_size} "
                f"exceeds limit {max_bytes}[/yellow]"
            )
            await _mark_version_expanded(
                task_id=task_id,
                version=version,
                manifest_key=None,
            )
            return summary

        existing = await _maybe_short_circuit_on_manifest(
            storage, manifest_key=manifest_key, archive_etag=archive_etag
        )
        if existing is not None:
            console.print(
                f"[green]TASK_EXPAND short-circuit: manifest in sync "
                f"(archive_etag={archive_etag})[/green]"
            )
            await _mark_version_expanded(
                task_id=task_id,
                version=version,
                manifest_key=manifest_key,
            )
            return {
                "status": "already_expanded",
                "files": int(existing.get("files_count", len(existing.get("files", []) or []))),
                "archive_etag": archive_etag,
            }

        # Load the archive once (goes through the Phase-0 cache so a
        # later read doesn't re-download).
        archive_bytes, _members = await storage._load_task_archive(archive_key)

        members = _parse_members(archive_bytes)
        max_member = int(settings.tasks_expand_max_member_bytes)

        manifest_files: list[dict[str, object]] = []
        upload_plan: list[tuple[tarfile.TarInfo, str, str, bytes, str]] = []

        for member in members:
            normalized = normalize_s3_relative_path(member.name)
            if not normalized:
                continue
            size = int(member.size or 0)
            if max_member and size > max_member:
                manifest_files.append(
                    {
                        "path": normalized,
                        "size": size,
                        "skipped": True,
                        "skip_reason": "member_too_large",
                    }
                )
                continue
            body = _read_member_bytes(archive_bytes, member.name)
            digest = hashlib.sha256(body).hexdigest()
            content_type, _ = mimetypes.guess_type(normalized)
            target_key = f"{expanded_prefix}{normalized}"
            upload_plan.append((member, normalized, target_key, body, content_type or ""))
            manifest_files.append(
                {
                    "path": normalized,
                    "size": size,
                    "sha256": digest,
                }
            )

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_MEMBER_UPLOADS)

        async def _upload_one(
            _member: tarfile.TarInfo,
            _path: str,
            target_key: str,
            body: bytes,
            content_type: str,
        ) -> None:
            async with semaphore:
                await storage.upload_bytes(
                    body,
                    target_key,
                    content_type=content_type or None,
                )

        await asyncio.gather(*(_upload_one(*item) for item in upload_plan))

        manifest_payload = {
            "task_id": task_id,
            "version": version,
            "archive_key": archive_key,
            "archive_etag": archive_etag,
            "archive_size": archive_size,
            "expanded_at": datetime.now(timezone.utc).isoformat(),
            "files_count": len(manifest_files),
            "files": manifest_files,
        }
        manifest_bytes = json.dumps(manifest_payload, sort_keys=True).encode("utf-8")
        await storage.upload_bytes(
            manifest_bytes, manifest_key, content_type="application/json"
        )

        await _mark_version_expanded(
            task_id=task_id,
            version=version,
            manifest_key=manifest_key,
        )

        summary = {
            "status": "expanded",
            "files": len([f for f in manifest_files if not f.get("skipped")]),
            "skipped": len([f for f in manifest_files if f.get("skipped")]),
            "archive_etag": archive_etag,
        }
        console.print(
            f"[green]TASK_EXPAND {task_id} v{version} done: {summary}[/green]"
        )
        return summary
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            await asyncio.gather(heartbeat_task, return_exceptions=True)


async def _mark_version_expanded(
    *,
    task_id: str,
    version: int,
    manifest_key: str | None,
) -> None:
    """Stamp ``expanded_at`` / ``expanded_manifest_key`` on the version row.

    Tolerates missing rows so a mid-flight backfill of a deleted task
    doesn't fail the job.
    """
    version_id = f"{task_id}-v{version}"
    async with get_session() as session:
        row = await session.get(TaskVersionModel, version_id)
        if row is None:
            # Fall back to (task_id, version) lookup for callers that
            # don't follow the {task_id}-v{version} id convention.
            row = await session.scalar(
                select(TaskVersionModel).where(
                    TaskVersionModel.task_id == task_id,
                    TaskVersionModel.version == version,
                )
            )
        if row is None:
            return
        row.expanded_at = utcnow()
        row.expanded_manifest_key = manifest_key
        await session.commit()

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
from typing import cast

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


async def _resolve_archive_key(
    storage: StorageClient, task_id: str, version: int
) -> str:
    """Return the S3 key of a task's archive, with legacy fallback.

    Mirrors the read-path behavior in ``StorageClient._resolve_task_prefix``:
    pre-versioning uploads landed at ``tasks/{task_id}/.oddish-task.tar.gz``
    (no ``v{N}/`` sub-prefix) and still need to be expandable. If the
    versioned key doesn't exist we fall back to the unversioned one;
    if neither exists we surface the versioned key so the caller's
    404 error message points at the expected location.
    """
    _root, archive_key = await storage._resolve_task_prefix(task_id, version)
    return archive_key


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


async def _list_loose_task_files(
    storage: StorageClient, task_id: str
) -> list[dict[str, object]]:
    """List a pre-archive task's files directly under ``tasks/{task_id}/``.

    Used when no ``.oddish-task.tar.gz`` exists anywhere for the task.
    These uploads went through the old ``upload_task_directory()`` flow
    that wrote loose objects to S3 instead of a single tarball. Filters
    out trial artifacts (``trials/``), versioned sub-prefixes (``v{N}/``,
    ``v{N}-files/``), and the archive sentinel itself so the result is
    exactly the task's source tree.
    """
    root = f"tasks/{task_id}/"
    objects = await storage.list_objects_all(root)
    out: list[dict[str, object]] = []
    for obj in objects:
        key = str(obj.get("key") or "")
        if not key.startswith(root):
            continue
        rel = key[len(root) :]
        if not rel or rel.endswith("/"):
            continue
        first = rel.split("/", 1)[0]
        if first == "trials":
            continue
        # Skip anything already inside a versioned sub-prefix
        # (``v1/``, ``v1-files/``, ``v2/`` ...) so we never accidentally
        # surface another version's contents as this version's files.
        if first and first[0] == "v":
            tail = first[1:].split("-", 1)[0]
            if tail.isdigit():
                continue
        if rel == StorageClient._TASK_ARCHIVE_OBJECT_NAME:
            continue
        out.append(
            {
                "relative_path": rel,
                "source_key": key,
                "size": int(obj.get("size") or 0),
            }
        )
    return out


async def _migrate_loose_task_files(
    storage: StorageClient,
    *,
    task_id: str,
    version: int,
    expanded_prefix: str,
    manifest_key: str,
    loose_files: list[dict[str, object]],
) -> dict:
    """Copy a loose-file task into the unified ``v{N}-files/`` layout.

    For pre-archive tasks the source bytes already live as individual
    S3 objects, so this is the moral equivalent of the tar-extraction
    branch in ``run_task_expand_job`` — just with sources coming from
    separate objects instead of members of one tarball. Uses download +
    upload for maximum S3-compatible-backend support; the files are
    typically a handful of small configs / scripts so the extra
    round-trip versus a server-side ``CopyObject`` is negligible.
    """
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_MEMBER_UPLOADS)

    async def _copy_one(entry: dict[str, object]) -> dict[str, object]:
        source_key = str(entry["source_key"])
        rel_path = str(entry["relative_path"])
        async with semaphore:
            try:
                body = await storage.download_bytes(source_key)
                content_type, _ = mimetypes.guess_type(rel_path)
                target_key = f"{expanded_prefix}{rel_path}"
                await storage.upload_bytes(
                    body, target_key, content_type=content_type or None
                )
            except Exception as exc:
                # Supabase S3 (and other S3-compatible backends) reject
                # keys containing certain characters like ``[``, ``]``,
                # URL-encoded bytes, etc. Record the skip in the manifest
                # so the rest of the task still migrates instead of the
                # whole job 6x-retrying and permanently FAILING.
                return {
                    "path": rel_path,
                    "size": int(cast(int, entry.get("size") or 0)),
                    "skipped": True,
                    "skip_reason": (
                        f"upload_failed: {type(exc).__name__}: " f"{str(exc)[:200]}"
                    ),
                    "source_key": source_key,
                }
        return {
            "path": rel_path,
            "size": int(cast(int, entry.get("size") or 0)) or len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "source_key": source_key,
        }

    manifest_files = list(await asyncio.gather(*(_copy_one(f) for f in loose_files)))

    manifest_payload = {
        "task_id": task_id,
        "version": version,
        "source": "loose_files",
        "source_prefix": f"tasks/{task_id}/",
        "expanded_at": datetime.now(timezone.utc).isoformat(),
        "files_count": len(manifest_files),
        "files": manifest_files,
    }
    manifest_bytes = json.dumps(manifest_payload, sort_keys=True, default=str).encode(
        "utf-8"
    )
    await storage.upload_bytes(
        manifest_bytes, manifest_key, content_type="application/json"
    )
    return {
        "status": "expanded",
        "source": "loose_files",
        "files": len(manifest_files),
    }


def _extract_regular_members(
    archive_bytes: bytes,
    *,
    max_member_bytes: int,
) -> list[dict[str, object]]:
    """Stream every regular-file member out of the tarball in a single pass.

    Returns a list of ``{"name", "size", "body", "skipped", "skip_reason"}``
    dicts. Oversize members carry ``skipped=True`` and ``body=b""`` so the
    caller can emit a manifest entry without ever touching their bytes.

    A previous version extracted per-member by re-opening the gzip stream
    from the start, which was O(N^2) in decompression cost. The iterator
    below walks each tar record once.
    """
    members: list[dict[str, object]] = []
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            size = int(member.size or 0)
            if max_member_bytes and size > max_member_bytes:
                members.append(
                    {
                        "name": member.name,
                        "size": size,
                        "body": b"",
                        "skipped": True,
                        "skip_reason": "member_too_large",
                    }
                )
                # ``tar`` streams sequentially; advancing past the
                # skipped body is handled by the next iteration
                # because tarfile discards unread member data on the
                # next ``next()`` call.
                continue
            extracted = tar.extractfile(member)
            body = extracted.read() if extracted is not None else b""
            members.append(
                {
                    "name": member.name,
                    "size": size,
                    "body": body,
                    "skipped": False,
                }
            )
    return members


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
    archive_key = await _resolve_archive_key(storage, task_id, version)
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
            # No archive. Pre-archive uploads via ``upload_task_directory``
            # stored loose per-file S3 objects at ``tasks/{task_id}/``;
            # migrate those directly into the same ``v{N}-files/`` layout
            # so the reader has one canonical fast path for every task.
            # A short-circuit on an existing manifest keeps re-runs cheap
            # (pre-archive tasks are frozen, so the stale-manifest risk
            # is nil).
            if await storage.object_exists(manifest_key):
                await _mark_version_expanded(
                    task_id=task_id,
                    version=version,
                    manifest_key=manifest_key,
                )
                return {
                    "status": "already_expanded",
                    "source": "loose_files",
                }

            loose_files = await _list_loose_task_files(storage, task_id)
            if not loose_files:
                raise RuntimeError(
                    f"Task {task_id} v{version}: no archive at {archive_key} "
                    f"and no loose files under tasks/{task_id}/"
                ) from exc

            result = await _migrate_loose_task_files(
                storage,
                task_id=task_id,
                version=version,
                expanded_prefix=expanded_prefix,
                manifest_key=manifest_key,
                loose_files=loose_files,
            )
            await _mark_version_expanded(
                task_id=task_id,
                version=version,
                manifest_key=manifest_key,
            )
            console.print(
                f"[green]TASK_EXPAND {task_id} v{version} "
                f"(loose_files): {result}[/green]"
            )
            return result

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
            # Intentionally leave ``expanded_at`` NULL so
            # ``/admin/tasks/expand-backfill`` (which filters on
            # ``expanded_at IS NULL``) re-picks this version if the
            # operator raises ``tasks_expand_max_bytes`` later. The
            # worker-job outcome is still SUCCESS — expansion was
            # skipped by policy, not failed.
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
                "files": int(
                    existing.get("files_count", len(existing.get("files", []) or []))
                ),
                "archive_etag": archive_etag,
            }

        # Load the archive once (goes through the Phase-0 cache so a
        # later read doesn't re-download).
        archive_bytes, _members = await storage._load_task_archive(archive_key)

        max_member = int(settings.tasks_expand_max_member_bytes)
        extracted_members = _extract_regular_members(
            archive_bytes, max_member_bytes=max_member
        )

        manifest_files: list[dict[str, object]] = []
        # Upload plan entries carry the manifest index so a rejected
        # upload can swap the success-shaped manifest entry for a skip
        # entry without unbalancing the list.
        upload_plan: list[tuple[int, str, bytes, str]] = []

        for entry in extracted_members:
            normalized = normalize_s3_relative_path(str(entry["name"]))
            if not normalized:
                continue
            size = int(cast(int, entry["size"]))
            if entry.get("skipped"):
                manifest_files.append(
                    {
                        "path": normalized,
                        "size": size,
                        "skipped": True,
                        "skip_reason": str(entry.get("skip_reason", "skipped")),
                    }
                )
                continue
            body = entry["body"]  # type: ignore[assignment]
            if not isinstance(body, (bytes, bytearray)):
                body = b""
            digest = hashlib.sha256(body).hexdigest()
            content_type, _ = mimetypes.guess_type(normalized)
            target_key = f"{expanded_prefix}{normalized}"
            idx = len(manifest_files)
            manifest_files.append(
                {
                    "path": normalized,
                    "size": size,
                    "sha256": digest,
                }
            )
            upload_plan.append((idx, target_key, bytes(body), content_type or ""))

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_MEMBER_UPLOADS)

        async def _upload_one(
            idx: int,
            target_key: str,
            body: bytes,
            content_type: str,
        ) -> tuple[int, str] | None:
            async with semaphore:
                try:
                    await storage.upload_bytes(
                        body,
                        target_key,
                        content_type=content_type or None,
                    )
                    return None
                except Exception as exc:
                    # Per-file tolerance: a single unacceptable key
                    # (e.g. Supabase rejects ``[brackets]`` or URL-
                    # encoded bytes) shouldn't bury a 300-file task.
                    return (
                        idx,
                        (f"upload_failed: {type(exc).__name__}: " f"{str(exc)[:200]}"),
                    )

        failures = await asyncio.gather(*(_upload_one(*item) for item in upload_plan))
        for fail in failures:
            if fail is None:
                continue
            failed_idx, reason = fail
            original = manifest_files[failed_idx]
            manifest_files[failed_idx] = {
                "path": original["path"],
                "size": original["size"],
                "skipped": True,
                "skip_reason": reason,
            }

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

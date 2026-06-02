from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import io
import json
import posixpath
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException
from oddish.config import settings

WORKER_TASK_MOUNT_PATH = Path("/mnt/oddish-tasks")
WORKER_TASK_KEY_PREFIX = "tasks/"


def _cleanup_temp_directory(path: Path | None) -> None:
    """Best-effort removal for temporary S3 download directories."""
    if path is None:
        return
    shutil.rmtree(path, ignore_errors=True)


def normalize_s3_relative_path(value: str | None) -> str:
    """Normalize and validate an S3 relative path."""
    if not value:
        return ""
    raw = value.replace("\\", "/")
    if raw.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    parts = PurePosixPath(raw).parts
    if ".." in parts:
        raise HTTPException(status_code=400, detail="Invalid path")
    normalized = posixpath.normpath(raw)
    if normalized in ("", "."):
        return ""
    if normalized.startswith("../") or normalized == "..":
        raise HTTPException(status_code=400, detail="Invalid path")
    return normalized.lstrip("/")


def extract_s3_key_from_path(path: str | None) -> str | None:
    """Return the S3 key for an s3:// path, if present."""
    if not path:
        return None
    if path.startswith("s3://"):
        return path[5:]
    return None


def _validate_task_archive_members(
    members: list[tarfile.TarInfo], destination: Path
) -> None:
    # Resolve the destination so the containment check compares like for
    # like. On macOS tempfile.mkdtemp() returns a /var/folders path that
    # symlinks into /private/var/folders; member paths resolve through
    # that symlink, so an unresolved destination would never appear in
    # resolved.parents and every extraction would falsely trip the
    # path-traversal guard below.
    destination = destination.resolve()
    for member in members:
        if member.islnk() or member.issym():
            raise ValueError("links not allowed")
        member_path = Path(member.name)
        if member_path.is_absolute():
            raise ValueError("absolute paths not allowed")
        resolved = (destination / member.name).resolve()
        if destination not in resolved.parents and resolved != destination:
            raise ValueError("path traversal")


def extract_task_tarfile(tar: tarfile.TarFile, destination: Path) -> None:
    """Safely extract a task tarball into a destination directory."""
    members = tar.getmembers()
    _validate_task_archive_members(members, destination)
    for member in members:
        tar.extract(member, path=destination, filter="data")


def _task_archive_members_from_bytes(archive_bytes: bytes) -> list[dict[str, object]]:
    files: dict[str, dict[str, object]] = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            normalized_path = normalize_s3_relative_path(member.name)
            if not normalized_path:
                continue
            files[normalized_path] = {
                "path": normalized_path,
                "size": member.size,
                "last_modified": datetime.fromtimestamp(member.mtime, tz=timezone.utc),
            }
    return [files[path] for path in sorted(files)]


def _read_task_archive_text(archive_bytes: bytes, file_path: str) -> str:
    normalized_path = normalize_s3_relative_path(file_path)
    if not normalized_path:
        raise HTTPException(status_code=400, detail="Invalid file path")

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            member_path = normalize_s3_relative_path(member.name)
            if member_path != normalized_path:
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                break
            return extracted.read().decode("utf-8")

    raise HTTPException(
        status_code=404, detail=f"Task file not found: {normalized_path}"
    )


class StorageClient:
    """
    Async S3-compatible storage client.

    Supports AWS S3, Supabase Storage, MinIO, and any S3-compatible backend.
    """

    def __init__(self):
        self._client: aioboto3.Client | None = None
        self._session: aioboto3.Session | None = None

    @property
    def _s3(self) -> aioboto3.Client:
        assert self._client is not None, "call _ensure_client() first"
        return self._client  # type: ignore[return-value]

    _MAX_CONCURRENT_UPLOADS = 8
    _TASK_ARCHIVE_OBJECT_NAME = ".oddish-task.tar.gz"
    _EXPANDED_MANIFEST_OBJECT_NAME = ".oddish-manifest.json"
    # Staging archive written by ``oddish import`` clients via a presigned
    # PUT; the complete endpoint extracts it into the trial prefix and
    # deletes the staging object.
    _TRIAL_IMPORT_ARCHIVE_OBJECT_NAME = ".oddish-trial-import.tar.gz"

    # Per-process archive cache: maps (archive_key, etag) -> (bytes, members)
    # where members is the list produced by ``_task_archive_members_from_bytes``.
    # Size-bounded in total decompressed byte footprint so a single task doesn't
    # blow the limit. Keys without a known etag fall back to
    # ``(content_length, last_modified)``.
    _archive_cache: (
        "OrderedDict[tuple[str, str], tuple[bytes, list[dict[str, object]]]]"
    ) = OrderedDict()
    _archive_cache_bytes: int = 0

    @classmethod
    def _archive_cache_capacity_bytes(cls) -> int:
        mb = getattr(settings, "tasks_archive_cache_mb", 256) or 0
        return max(int(mb), 0) * 1024 * 1024

    @classmethod
    def _archive_cache_get(
        cls, cache_key: tuple[str, str]
    ) -> tuple[bytes, list[dict[str, object]]] | None:
        entry = cls._archive_cache.get(cache_key)
        if entry is None:
            return None
        cls._archive_cache.move_to_end(cache_key)
        return entry

    @classmethod
    def _archive_cache_put(
        cls,
        cache_key: tuple[str, str],
        value: tuple[bytes, list[dict[str, object]]],
    ) -> None:
        capacity = cls._archive_cache_capacity_bytes()
        if capacity <= 0:
            return
        size = len(value[0])
        if size > capacity:
            return
        existing = cls._archive_cache.pop(cache_key, None)
        if existing is not None:
            cls._archive_cache_bytes -= len(existing[0])
        cls._archive_cache[cache_key] = value
        cls._archive_cache_bytes += size
        while cls._archive_cache_bytes > capacity and cls._archive_cache:
            _, evicted = cls._archive_cache.popitem(last=False)
            cls._archive_cache_bytes -= len(evicted[0])

    async def _head_archive_cache_key(self, archive_key: str) -> tuple[str, str] | None:
        """Build a stable cache key for an archive object.

        Uses the ETag when the backend returns one (AWS S3, MinIO). Falls back
        to ``(ContentLength, LastModified)`` so S3-compatible backends that
        drop the ETag still get a working cache. Returns ``None`` for any
        backend / fake that doesn't expose ``head_object`` (e.g. test
        doubles), which disables caching for that call without breaking
        the read path.
        """
        await self._ensure_client()
        head_fn = getattr(self._s3, "head_object", None)
        if head_fn is None:
            return None
        try:
            head = await head_fn(Bucket=settings.s3_bucket, Key=archive_key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        except Exception:
            # A fake client or transient error shouldn't poison the
            # primary read path; fall through to the uncached download.
            return None

        etag = head.get("ETag")
        if etag:
            return (archive_key, str(etag))
        length = head.get("ContentLength")
        last_modified = head.get("LastModified")
        fallback = f"{length}:{last_modified}"
        return (archive_key, fallback)

    # archive_key -> etag-or-fallback from the most recent successful
    # ``_head_archive_cache_key`` call. Lets ``_head_archive_etag`` reuse
    # the HEAD that ``_load_task_archive`` already issued for the cache
    # lookup so a single read path doesn't pay two HEADs per file click.
    # Immutable-per-version archives mean stale hints are only possible
    # if an archive is rewritten out-of-band, which never happens in the
    # current upload flow; ``_load_task_archive`` refreshes the hint on
    # every call regardless.
    _archive_etag_hints: "OrderedDict[str, str]" = OrderedDict()
    _ARCHIVE_ETAG_HINTS_MAX = 1024

    @classmethod
    def _remember_archive_etag(cls, archive_key: str, etag: str) -> None:
        cls._archive_etag_hints[archive_key] = etag
        cls._archive_etag_hints.move_to_end(archive_key)
        while len(cls._archive_etag_hints) > cls._ARCHIVE_ETAG_HINTS_MAX:
            cls._archive_etag_hints.popitem(last=False)

    async def _load_task_archive(
        self, archive_key: str
    ) -> tuple[bytes, list[dict[str, object]]]:
        """Fetch and parse a task archive, preferring the in-process cache.

        Callers that need both the listing and a member body can reuse the
        returned ``(bytes, members)`` tuple without re-downloading or
        re-decompressing the tarball. As a side effect the archive's
        ETag (or ``(length, last_modified)`` fallback) is stashed so a
        subsequent ``_head_archive_etag`` call doesn't re-issue HEAD.
        """
        cache_key = await self._head_archive_cache_key(archive_key)
        if cache_key is not None:
            self._remember_archive_etag(archive_key, cache_key[1])
            cached = self._archive_cache_get(cache_key)
            if cached is not None:
                return cached

        archive_bytes = await self.download_bytes(archive_key)
        members = _task_archive_members_from_bytes(archive_bytes)
        value = (archive_bytes, members)
        if cache_key is not None:
            self._archive_cache_put(cache_key, value)
        return value

    async def _head_archive_etag(self, archive_key: str) -> str | None:
        """Return the current ETag for an archive object, if the backend exposes it.

        Reuses the hint populated by a recent ``_load_task_archive``
        call so the common "list + read one file" flow does a single
        HEAD instead of two.
        """
        hinted = self._archive_etag_hints.get(archive_key)
        if hinted is not None:
            return hinted
        cache_key = await self._head_archive_cache_key(archive_key)
        if cache_key is None:
            return None
        self._remember_archive_etag(archive_key, cache_key[1])
        return cache_key[1]

    async def _ensure_client(self):
        """Lazy initialization of aioboto3 client."""
        if self._client is not None:
            return

        self._session = aioboto3.Session()
        self._client = await self._session.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(signature_version="s3v4"),
        ).__aenter__()

    async def close(self):
        """Close the S3 client."""
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None

    # =========================================================================
    # High-level operations
    # =========================================================================

    @staticmethod
    def _trial_prefix(trial_id: str) -> str:
        """
        Return an S3 prefix for trial artifacts.

        Prefer nesting under the task prefix (tasks/<task_id>/trials/<trial_id>/)
        because some S3 backends/policies only allow writes under "tasks/".
        """
        # trial_id is generated as f"{task_id}-{i}" where i is an integer index.
        # task_id itself can contain dashes, so split from the right.
        task_id, sep, maybe_index = trial_id.rpartition("-")
        if sep and maybe_index.isdigit() and task_id:
            return f"tasks/{task_id}/trials/{trial_id}/"
        return f"trials/{trial_id}/"

    async def upload_task_directory(self, task_id: str, local_path: Path) -> str:
        """
        Upload a task directory to S3.

        Args:
            task_id: Unique task identifier
            local_path: Local path to the task directory

        Returns:
            S3 key prefix for the uploaded task
        """
        await self._ensure_client()
        s3_prefix = f"tasks/{task_id}/"

        if not local_path.exists() or not local_path.is_dir():
            raise ValueError(f"Task directory does not exist: {local_path}")

        await self._upload_directory(local_path, s3_prefix)

        return s3_prefix

    @classmethod
    def _task_archive_key(cls, task_id: str) -> str:
        return f"tasks/{task_id}/{cls._TASK_ARCHIVE_OBJECT_NAME}"

    @classmethod
    def _task_archive_key_from_prefix(cls, s3_prefix: str) -> str:
        normalized_prefix = normalize_s3_prefix(s3_prefix)
        if not normalized_prefix:
            raise ValueError(f"Invalid task S3 prefix: {s3_prefix}")
        return f"{normalized_prefix}{cls._TASK_ARCHIVE_OBJECT_NAME}"

    async def _resolve_task_prefix(
        self, task_id: str, version: int | None
    ) -> tuple[str, str]:
        """Return ``(root_prefix, archive_key)`` for a task, with fallback.

        When *version* is given the versioned path is tried first.  If the
        archive doesn't exist there (backfilled v1 tasks), falls back to the
        unversioned ``tasks/{task_id}/`` path.
        """
        if version is not None:
            vroot = f"tasks/{task_id}/v{version}/"
            varchive = f"{vroot}{self._TASK_ARCHIVE_OBJECT_NAME}"
            if await self.object_exists(varchive):
                return vroot, varchive
            # Check if unversioned prefix has the archive instead (backfill)
            fallback_root = f"tasks/{task_id}/"
            fallback_archive = self._task_archive_key(task_id)
            if await self.object_exists(fallback_archive):
                return fallback_root, fallback_archive
            # Neither exists; return the versioned one so callers get a
            # consistent "not found" behaviour for the S3-objects-based path.
            return vroot, varchive

        return f"tasks/{task_id}/", self._task_archive_key(task_id)

    async def download_task_directory(self, s3_prefix: str, local_path: Path) -> None:
        """
        Download a task directory from S3.

        Args:
            s3_prefix: S3 key prefix (e.g., "tasks/abc123/")
            local_path: Local path where to download the task
        """
        await self._ensure_client()
        local_path.mkdir(parents=True, exist_ok=True)

        archive_key = self._task_archive_key_from_prefix(s3_prefix)
        if await self.object_exists(archive_key):
            await self._download_and_extract_task_archive(archive_key, local_path)
            return

        # Collect all objects under the prefix.  If one of them is a task
        # archive at a versioned sub-path (e.g. tasks/{id}/v1/.oddish-task.tar.gz)
        # extract it instead of downloading the tarball as a raw file.
        nested_archive_key: str | None = None
        non_archive_objects: list[dict] = []

        paginator = self._s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=settings.s3_bucket, Prefix=s3_prefix
        ):
            for obj in page.get("Contents", []):
                s3_key = obj["Key"]
                if s3_key.endswith(f"/{self._TASK_ARCHIVE_OBJECT_NAME}"):
                    if nested_archive_key is None or s3_key > nested_archive_key:
                        nested_archive_key = s3_key
                else:
                    non_archive_objects.append(obj)

        if nested_archive_key is not None:
            await self._download_and_extract_task_archive(
                nested_archive_key, local_path
            )
            return

        for obj in non_archive_objects:
            s3_key = obj["Key"]
            relative_path = s3_key[len(s3_prefix) :]
            if not relative_path:
                continue
            local_file = local_path / relative_path
            local_file.parent.mkdir(parents=True, exist_ok=True)
            await self.download_file(s3_key, local_file)

    async def upload_trial_results(self, trial_id: str, harbor_job_dir: Path) -> str:
        """
        Upload Harbor trial results to S3.

        Args:
            trial_id: Trial identifier
            harbor_job_dir: Local path to Harbor job results

        Returns:
            S3 key prefix for the uploaded trial
        """
        await self._ensure_client()
        s3_prefix = self._trial_prefix(trial_id)

        if not harbor_job_dir.exists():
            raise ValueError(f"Harbor job directory does not exist: {harbor_job_dir}")

        await self._upload_directory(harbor_job_dir, s3_prefix)

        return s3_prefix

    @classmethod
    def _trial_import_archive_key(cls, trial_id: str) -> str:
        """Key for the staging tarball uploaded during ``oddish import``."""
        prefix = cls._trial_prefix(trial_id)
        return f"{prefix}{cls._TRIAL_IMPORT_ARCHIVE_OBJECT_NAME}"

    async def extract_trial_import_archive(self, trial_id: str) -> int:
        """Extract a previously-uploaded trial import tarball into the trial prefix.

        Expected flow:

        1. ``/trials/import/init`` returns a presigned PUT URL for the
           key returned by ``_trial_import_archive_key(trial_id)``.
        2. Client tars the harbor trial subdir and PUTs it to that URL.
        3. ``/trials/import/complete`` calls this method, which downloads
           the staging object, extracts it into the trial prefix
           (``tasks/<task_id>/trials/<trial_id>/``), and deletes the
           staging object. The individual files are what the existing
           ``/trials/<id>/logs|result|trajectory`` endpoints read, so no
           other plumbing needs to know an import happened.

        Returns the number of files extracted.
        """
        await self._ensure_client()
        archive_key = self._trial_import_archive_key(trial_id)
        s3_prefix = self._trial_prefix(trial_id)

        if not await self.object_exists(archive_key):
            raise HTTPException(
                status_code=400,
                detail="Uploaded trial archive not found in S3",
            )

        archive_bytes = await self.download_bytes(archive_key)

        extracted = 0
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            # Path-traversal protection: reject absolute paths, symlinks,
            # and ``..`` segments that would escape the prefix.
            for member in members:
                if member.issym() or member.islnk():
                    raise HTTPException(
                        status_code=400, detail="links not allowed in trial archive"
                    )
                normalized = normalize_s3_relative_path(member.name)
                if not normalized:
                    continue
                extracted_file = tar.extractfile(member)
                if extracted_file is None:
                    continue
                data = extracted_file.read()
                s3_key = f"{s3_prefix}{normalized}"
                await self._s3.put_object(
                    Bucket=settings.s3_bucket,
                    Key=s3_key,
                    Body=data,
                )
                extracted += 1

        # Best-effort cleanup of the staging object. Failing to delete it
        # is not fatal -- the trial row already points at the prefix.
        try:
            await self._s3.delete_object(
                Bucket=settings.s3_bucket,
                Key=archive_key,
            )
        except Exception:
            pass

        return extracted

    async def _upload_directory(self, local_path: Path, s3_prefix: str) -> None:
        """Upload a directory tree to S3 with bounded concurrency."""
        file_paths = [path for path in local_path.rglob("*") if path.is_file()]
        if not file_paths:
            return

        semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_UPLOADS)

        async def upload_one(file_path: Path) -> None:
            relative_path = file_path.relative_to(local_path)
            s3_key = f"{s3_prefix}{relative_path}"
            async with semaphore:
                await self.upload_file(file_path, s3_key)

        await asyncio.gather(*(upload_one(file_path) for file_path in file_paths))

    async def download_trial_directory(self, s3_prefix: str, local_path: Path) -> None:
        """
        Download a trial directory from S3.

        Args:
            s3_prefix: S3 key prefix for the trial (e.g., "tasks/abc123/trials/abc123-0/")
            local_path: Local path where to download the trial results
        """
        await self._ensure_client()
        local_path.mkdir(parents=True, exist_ok=True)

        paginator = self._s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=settings.s3_bucket, Prefix=s3_prefix
        ):
            for obj in page.get("Contents", []):
                s3_key = obj["Key"]
                relative_path = s3_key[len(s3_prefix) :]
                if not relative_path:
                    continue
                local_file = local_path / relative_path
                local_file.parent.mkdir(parents=True, exist_ok=True)
                await self.download_file(s3_key, local_file)

    async def download_trial_logs(self, s3_prefix: str) -> str:
        """
        Download and concatenate trial logs from S3.

        Args:
            s3_prefix: S3 key prefix for the trial (e.g., "trials/abc-0/")

        Returns:
            Concatenated log content as string
        """
        await self._ensure_client()
        logs = []

        paginator = self._s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=settings.s3_bucket, Prefix=s3_prefix
        ):
            for obj in page.get("Contents", []):
                s3_key = obj["Key"]
                # Match local log selection: *.log, *.txt, or files in logs/agent/verifier
                s3_path = Path(s3_key)
                is_log_file = s3_path.suffix in (".log", ".txt")
                is_log_dir = any(
                    part in s3_path.parts for part in ("logs", "agent", "verifier")
                )
                if not is_log_file and not is_log_dir:
                    continue
                if s3_path.suffix in (".json", ".patch"):
                    continue
                content = await self.download_text(s3_key)
                logs.append(f"=== {s3_key} ===\n{content}\n")

        return "\n".join(logs) if logs else ""

    async def list_task_files(
        self,
        *,
        task_id: str,
        prefix: str | None,
        recursive: bool,
        limit: int,
        cursor: str | None,
        presign: bool,
        presign_expiration: int = 900,
        version: int | None = None,
    ) -> dict:
        """List files in a task's S3 directory.

        When *version* is given the versioned prefix ``tasks/{task_id}/v{version}/``
        is tried first.  If it doesn't exist (e.g. backfilled v1 tasks whose
        files live at the unversioned ``tasks/{task_id}/`` prefix) the method
        falls back automatically.
        """
        # Prefer the per-file expanded layout when available. Sibling prefix
        # (``v{N}-files/``) isolates expansion artifacts from user files.
        if version is not None:
            expanded_prefix = f"tasks/{task_id}/v{version}-files/"
            manifest_key = f"{expanded_prefix}{self._EXPANDED_MANIFEST_OBJECT_NAME}"
            if await self.object_exists(manifest_key):
                return await self._list_expanded_task_files(
                    task_id=task_id,
                    expanded_prefix=expanded_prefix,
                    prefix=prefix,
                    recursive=recursive,
                    limit=limit,
                    cursor=cursor,
                    presign=presign,
                    presign_expiration=presign_expiration,
                )

        root_prefix, archive_key = await self._resolve_task_prefix(task_id, version)
        if await self.object_exists(archive_key):
            archive_bytes, archive_files = await self._load_task_archive(archive_key)
            relative_prefix = normalize_s3_relative_path(prefix)
            if relative_prefix and not relative_prefix.endswith("/"):
                relative_prefix = f"{relative_prefix}/"
            full_prefix = f"{root_prefix}{relative_prefix}"

            filtered_files = [
                file_meta
                for file_meta in archive_files
                if not relative_prefix
                or str(file_meta["path"]).startswith(relative_prefix)
            ]
            archive_url = (
                await self.get_presigned_url(archive_key, expiration=presign_expiration)
                if presign
                else None
            )
            if recursive:
                return {
                    "task_id": task_id,
                    "files": filtered_files,
                    "dirs": [],
                    "prefix": full_prefix,
                    "recursive": True,
                    "presigned": bool(archive_url),
                    "presign_expires_in": presign_expiration if archive_url else None,
                    "archive_key": archive_key,
                    "archive_url": archive_url,
                }

            offset = int(cursor or "0")
            files: list[dict[str, object]] = []
            dir_paths: set[str] = set()
            for file_meta in filtered_files:
                archive_path = str(file_meta["path"])
                remainder = (
                    archive_path[len(relative_prefix) :]
                    if relative_prefix
                    else archive_path
                )
                first_component, _, rest = remainder.partition("/")
                if rest:
                    dir_paths.add(
                        f"{relative_prefix}{first_component}".rstrip("/")
                        if relative_prefix
                        else first_component
                    )
                    continue
                files.append(file_meta)

            entries: list[tuple[str, dict[str, object]]] = [
                ("dir", {"path": path}) for path in sorted(dir_paths)
            ]
            entries.extend(("file", file_meta) for file_meta in files)
            entries.sort(key=lambda item: str(item[1]["path"]))

            page = entries[offset : offset + limit]
            next_offset = offset + limit
            return {
                "task_id": task_id,
                "files": [entry for kind, entry in page if kind == "file"],
                "dirs": [entry for kind, entry in page if kind == "dir"],
                "prefix": full_prefix,
                "recursive": False,
                "cursor": str(next_offset) if next_offset < len(entries) else None,
                "truncated": next_offset < len(entries),
                "presigned": bool(archive_url),
                "presign_expires_in": presign_expiration if archive_url else None,
                "archive_key": archive_key,
                "archive_url": archive_url,
            }

        relative_prefix = normalize_s3_relative_path(prefix)
        if relative_prefix and not relative_prefix.endswith("/"):
            relative_prefix = f"{relative_prefix}/"
        full_prefix = f"{root_prefix}{relative_prefix}"

        if recursive:
            objects = await self.list_objects_all(full_prefix)
            files = []
            for obj in objects:
                key = obj.get("key")
                if not key:
                    continue
                relative_path = key[len(root_prefix) :]
                if relative_path:
                    files.append(
                        {
                            "path": relative_path,
                            "key": key,
                            "size": obj.get("size"),
                            "last_modified": obj.get("last_modified"),
                        }
                    )

            if presign and files:
                s3_keys = [str(f["key"]) for f in files]
                urls = await self.get_presigned_urls_batch(s3_keys, presign_expiration)
                for f in files:
                    key = str(f["key"])
                    f["url"] = urls.get(key)

            return {
                "task_id": task_id,
                "files": files,
                "dirs": [],
                "prefix": full_prefix,
                "recursive": True,
                "presigned": presign,
                "presign_expires_in": presign_expiration if presign else None,
            }

        listing = await self.list_objects(
            full_prefix,
            delimiter="/",
            max_keys=limit,
            continuation_token=cursor,
        )
        files = []
        for obj in listing["objects"]:
            key = obj.get("key")
            if not key:
                continue
            relative_path = key[len(root_prefix) :]
            if relative_path:
                files.append(
                    {
                        "path": relative_path,
                        "key": key,
                        "size": obj.get("size"),
                        "last_modified": obj.get("last_modified"),
                    }
                )

        if presign and files:
            s3_keys = [str(f["key"]) for f in files]
            urls = await self.get_presigned_urls_batch(s3_keys, presign_expiration)
            for f in files:
                key = str(f["key"])
                f["url"] = urls.get(key)

        dirs = []
        for common_prefix in listing["common_prefixes"]:
            if not common_prefix:
                continue
            relative_dir = common_prefix[len(root_prefix) :].rstrip("/")
            if relative_dir:
                dirs.append({"path": relative_dir})
        return {
            "task_id": task_id,
            "files": files,
            "dirs": dirs,
            "prefix": full_prefix,
            "recursive": False,
            "cursor": listing["next_token"],
            "truncated": listing["is_truncated"],
            "presigned": presign,
            "presign_expires_in": presign_expiration if presign else None,
        }

    async def _list_expanded_task_files(
        self,
        *,
        task_id: str,
        expanded_prefix: str,
        prefix: str | None,
        recursive: bool,
        limit: int,
        cursor: str | None,
        presign: bool,
        presign_expiration: int,
    ) -> dict:
        """List files in a task's expanded per-file S3 layout.

        The manifest sentinel (``.oddish-manifest.json``) is filtered out so
        callers never see the expansion bookkeeping. Response shape mirrors
        the archive branch so frontend logic is untouched, except
        ``archive_key`` / ``archive_url`` are omitted (each file has its own
        direct ``url`` instead).
        """
        relative_prefix = normalize_s3_relative_path(prefix)
        if relative_prefix and not relative_prefix.endswith("/"):
            relative_prefix = f"{relative_prefix}/"
        full_prefix = f"{expanded_prefix}{relative_prefix}"
        manifest_object = self._EXPANDED_MANIFEST_OBJECT_NAME

        if recursive:
            objects = await self.list_objects_all(full_prefix)
            files: list[dict[str, object]] = []
            for obj in objects:
                key = obj.get("key")
                if not key:
                    continue
                relative_path = key[len(expanded_prefix) :]
                if not relative_path or relative_path == manifest_object:
                    continue
                files.append(
                    {
                        "path": relative_path,
                        "key": key,
                        "size": obj.get("size"),
                        "last_modified": obj.get("last_modified"),
                    }
                )

            if presign and files:
                s3_keys = [str(f["key"]) for f in files]
                urls = await self.get_presigned_urls_batch(s3_keys, presign_expiration)
                for f in files:
                    key = str(f["key"])
                    f["url"] = urls.get(key)

            return {
                "task_id": task_id,
                "files": files,
                "dirs": [],
                "prefix": full_prefix,
                "recursive": True,
                "presigned": presign,
                "presign_expires_in": presign_expiration if presign else None,
            }

        listing = await self.list_objects(
            full_prefix,
            delimiter="/",
            max_keys=limit,
            continuation_token=cursor,
        )
        files = []
        for obj in listing["objects"]:
            key = obj.get("key")
            if not key:
                continue
            relative_path = key[len(expanded_prefix) :]
            if not relative_path or relative_path == manifest_object:
                continue
            files.append(
                {
                    "path": relative_path,
                    "key": key,
                    "size": obj.get("size"),
                    "last_modified": obj.get("last_modified"),
                }
            )

        if presign and files:
            s3_keys = [str(f["key"]) for f in files]
            urls = await self.get_presigned_urls_batch(s3_keys, presign_expiration)
            for f in files:
                key = str(f["key"])
                f["url"] = urls.get(key)

        dirs = []
        for common_prefix in listing["common_prefixes"]:
            if not common_prefix:
                continue
            relative_dir = common_prefix[len(expanded_prefix) :].rstrip("/")
            if relative_dir:
                dirs.append({"path": relative_dir})
        return {
            "task_id": task_id,
            "files": files,
            "dirs": dirs,
            "prefix": full_prefix,
            "recursive": False,
            "cursor": listing["next_token"],
            "truncated": listing["is_truncated"],
            "presigned": presign,
            "presign_expires_in": presign_expiration if presign else None,
        }

    async def list_trial_files(
        self,
        *,
        trial_id: str,
        prefix: str | None,
        recursive: bool,
        limit: int,
        cursor: str | None,
        presign: bool,
        presign_expiration: int = 900,
    ) -> dict:
        """List files in a trial's S3 directory."""
        root_prefix = self._trial_prefix(trial_id)
        relative_prefix = normalize_s3_relative_path(prefix)
        if relative_prefix and not relative_prefix.endswith("/"):
            relative_prefix = f"{relative_prefix}/"
        full_prefix = f"{root_prefix}{relative_prefix}"

        if recursive:
            objects = await self.list_objects_all(full_prefix)
            files = []
            for obj in objects:
                key = obj.get("key")
                if not key:
                    continue
                relative_path = key[len(root_prefix) :]
                if relative_path:
                    files.append(
                        {
                            "path": relative_path,
                            "key": key,
                            "size": obj.get("size"),
                            "last_modified": obj.get("last_modified"),
                        }
                    )

            if presign and files:
                s3_keys = [str(f["key"]) for f in files]
                urls = await self.get_presigned_urls_batch(s3_keys, presign_expiration)
                for f in files:
                    key = str(f["key"])
                    f["url"] = urls.get(key)

            return {
                "trial_id": trial_id,
                "files": files,
                "dirs": [],
                "prefix": full_prefix,
                "recursive": True,
                "presigned": presign,
                "presign_expires_in": presign_expiration if presign else None,
            }

        listing = await self.list_objects(
            full_prefix,
            delimiter="/",
            max_keys=limit,
            continuation_token=cursor,
        )
        files = []
        for obj in listing["objects"]:
            key = obj.get("key")
            if not key:
                continue
            relative_path = key[len(root_prefix) :]
            if relative_path:
                files.append(
                    {
                        "path": relative_path,
                        "key": key,
                        "size": obj.get("size"),
                        "last_modified": obj.get("last_modified"),
                    }
                )

        if presign and files:
            s3_keys = [str(f["key"]) for f in files]
            urls = await self.get_presigned_urls_batch(s3_keys, presign_expiration)
            for f in files:
                key = str(f["key"])
                f["url"] = urls.get(key)

        dirs = []
        for common_prefix in listing["common_prefixes"]:
            if not common_prefix:
                continue
            relative_dir = common_prefix[len(root_prefix) :].rstrip("/")
            if relative_dir:
                dirs.append({"path": relative_dir})
        return {
            "trial_id": trial_id,
            "files": files,
            "dirs": dirs,
            "prefix": full_prefix,
            "recursive": False,
            "cursor": listing["next_token"],
            "truncated": listing["is_truncated"],
            "presigned": presign,
            "presign_expires_in": presign_expiration if presign else None,
        }

    async def get_task_file_content(
        self,
        *,
        task_id: str,
        file_path: str,
        presign: bool,
        presign_expiration: int = 900,
        version: int | None = None,
    ) -> dict:
        """Get content of a specific task file from S3.

        Prefers the per-file expanded layout when the manifest sentinel
        exists under ``tasks/{id}/v{N}-files/``. Falls back to reading from
        the tarball otherwise. The response shape always includes ``path``
        and ``key``; ``content`` is populated for archive reads (or when
        ``presign=False`` on expanded) and ``url`` when ``presign=True`` on
        the expanded layout. When an archive is the source we additionally
        return ``archive_etag`` so HTTP layers can emit immutable
        ``ETag`` / ``Cache-Control`` headers.
        """
        normalized_path = normalize_s3_relative_path(file_path)
        if not normalized_path:
            raise HTTPException(status_code=400, detail="Invalid file path")

        if version is not None:
            expanded_prefix = f"tasks/{task_id}/v{version}-files/"
            manifest_key = f"{expanded_prefix}{self._EXPANDED_MANIFEST_OBJECT_NAME}"
            if await self.object_exists(manifest_key):
                s3_key = f"{expanded_prefix}{normalized_path}"
                # Some members may be absent from the expanded tree
                # (oversize-member skips, mid-flight expansions, or
                # ad-hoc object deletions). Check presence before
                # handing out a URL / downloading, and fall through to
                # the archive branch on miss so deep-links keep
                # working.
                if await self.object_exists(s3_key):
                    if presign:
                        url = await self.get_presigned_url(
                            s3_key, expiration=presign_expiration
                        )
                        return {
                            "path": normalized_path,
                            "key": s3_key,
                            "url": url,
                        }
                    content = await self.download_text(s3_key)
                    return {
                        "path": normalized_path,
                        "content": content,
                        "key": s3_key,
                    }

        root_prefix, archive_key = await self._resolve_task_prefix(task_id, version)
        if await self.object_exists(archive_key):
            archive_bytes, _members = await self._load_task_archive(archive_key)
            content = _read_task_archive_text(archive_bytes, normalized_path)
            archive_etag = await self._head_archive_etag(archive_key)
            return {
                "path": normalized_path,
                "content": content,
                "key": f"{archive_key}#{normalized_path}",
                "archive_key": archive_key,
                "archive_etag": archive_etag,
            }
        s3_key = f"{root_prefix}{normalized_path}"

        if presign:
            url = await self.get_presigned_url(s3_key, expiration=presign_expiration)
            return {"path": normalized_path, "key": s3_key, "url": url}
        content = await self.download_text(s3_key)
        return {"path": normalized_path, "content": content, "key": s3_key}

    async def get_trial_result_json(self, s3_prefix: str) -> dict | None:
        """
        Get the result.json from a trial's S3 storage.

        Args:
            s3_prefix: S3 key prefix for the trial

        Returns:
            Parsed result.json or None if not found
        """
        try:
            s3_key = f"{s3_prefix}result.json"
            return await self.download_json(s3_key)
        except Exception:
            return None

    # =========================================================================
    # Low-level S3 operations
    # =========================================================================

    async def upload_file(self, local_path: Path, s3_key: str) -> None:
        """Upload a file to S3."""
        await self._ensure_client()
        with open(local_path, "rb") as f:
            await self._s3.put_object(
                Bucket=settings.s3_bucket,
                Key=s3_key,
                Body=f,
            )

    async def upload_bytes(
        self,
        data: bytes,
        s3_key: str,
        *,
        content_type: str | None = None,
    ) -> None:
        """Upload an in-memory byte buffer to S3."""
        await self._ensure_client()
        extra: dict[str, str] = {}
        if content_type:
            extra["ContentType"] = content_type
        await self._s3.put_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
            Body=data,
            **extra,
        )

    async def download_file(self, s3_key: str, local_path: Path) -> None:
        """Download a file from S3."""
        await self._ensure_client()
        response = await self._s3.get_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
        )
        async with response["Body"] as stream:
            content = await stream.read()
            with open(local_path, "wb") as f:
                f.write(content)

    async def download_bytes(self, s3_key: str) -> bytes:
        """Download binary content from S3."""
        await self._ensure_client()
        response = await self._s3.get_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
        )
        async with response["Body"] as stream:
            content: bytes = await stream.read()
            return content

    async def download_text(self, s3_key: str) -> str:
        """Download text content from S3."""
        await self._ensure_client()
        response = await self._s3.get_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
        )
        async with response["Body"] as stream:
            content = await stream.read()
            result: str = content.decode("utf-8")
            return result

    async def download_json(self, s3_key: str) -> dict:
        """Download and parse JSON from S3."""
        await self._ensure_client()
        response = await self._s3.get_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
        )
        async with response["Body"] as stream:
            content = await stream.read()
            result: dict = json.loads(content.decode("utf-8"))
            return result

    async def object_exists(self, s3_key: str) -> bool:
        """Return whether an exact object key exists."""
        await self._ensure_client()
        try:
            await self._s3.head_object(Bucket=settings.s3_bucket, Key=s3_key)
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    async def list_keys(self, prefix: str) -> list[str]:
        """List all keys with a given prefix."""
        await self._ensure_client()
        keys = []
        paginator = self._s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every object stored under an S3 prefix."""
        await self._ensure_client()
        keys = await self.list_keys(prefix)
        if not keys:
            return 0

        deleted = 0
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            response = await self._s3.delete_objects(
                Bucket=settings.s3_bucket,
                Delete={
                    "Objects": [{"Key": key} for key in batch],
                    "Quiet": True,
                },
            )
            errors = response.get("Errors", [])
            if errors:
                first_error = errors[0]
                raise RuntimeError(
                    "Failed to delete S3 objects under prefix "
                    f"{prefix}: {first_error.get('Key')}: "
                    f"{first_error.get('Message', 'unknown error')}"
                )
            deleted += len(batch)
        return deleted

    async def delete_prefixes(self, prefixes: list[str]) -> int:
        """Delete objects for many prefixes, skipping duplicates."""
        deleted = 0
        for prefix in dict.fromkeys(prefixes):
            deleted += await self.delete_prefix(prefix)
        return deleted

    async def copy_object(self, src_key: str, dst_key: str) -> None:
        """Server-side copy of a single object within the bucket.

        Uses S3's ``CopyObject`` so the bytes are duplicated inside the
        storage backend without round-tripping through this process.
        """
        await self._ensure_client()
        await self._s3.copy_object(
            Bucket=settings.s3_bucket,
            Key=dst_key,
            CopySource={"Bucket": settings.s3_bucket, "Key": src_key},
        )

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
        """Recursively copy every object under ``src_prefix`` to ``dst_prefix``.

        Each source key has its ``src_prefix`` portion swapped for
        ``dst_prefix`` and is duplicated with a server-side copy. The
        ``.oddish-trial-import.tar.gz`` staging archive (if any lingered)
        is skipped so a combined trial never inherits a half-uploaded
        import tarball. Returns the number of objects copied.

        Copies run with bounded concurrency so an experiment with many
        large trials does not open an unbounded number of S3 calls.
        """
        src_prefix = normalize_s3_prefix(src_prefix) or src_prefix
        dst_prefix = normalize_s3_prefix(dst_prefix) or dst_prefix
        if src_prefix == dst_prefix:
            return 0

        keys = await self.list_keys(src_prefix)
        copyable = [
            key
            for key in keys
            if not key.endswith(f"/{self._TRIAL_IMPORT_ARCHIVE_OBJECT_NAME}")
        ]
        if not copyable:
            return 0

        semaphore = asyncio.Semaphore(16)

        async def _copy_one(src_key: str) -> None:
            relative = src_key[len(src_prefix) :]
            dst_key = f"{dst_prefix}{relative}"
            async with semaphore:
                await self.copy_object(src_key, dst_key)

        await asyncio.gather(*(_copy_one(key) for key in copyable))
        return len(copyable)

    async def prefix_exists(self, prefix: str) -> bool:
        """Return whether at least one object exists for a prefix."""
        await self._ensure_client()
        response = await self._s3.list_objects_v2(
            Bucket=settings.s3_bucket,
            Prefix=prefix,
            MaxKeys=1,
        )
        return bool(response.get("Contents"))

    async def list_objects_all(self, prefix: str) -> list[dict]:
        """List all objects with metadata (key, size, last_modified) for a given prefix."""
        await self._ensure_client()
        objects = []
        paginator = self._s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                objects.append(
                    {
                        "key": obj.get("Key"),
                        "size": obj.get("Size"),
                        "last_modified": obj.get("LastModified"),
                    }
                )
        return objects

    async def _download_and_extract_task_archive(
        self, archive_key: str, local_path: Path
    ) -> None:
        archive_bytes = await self.download_bytes(archive_key)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            extract_task_tarfile(tar, local_path)

    async def list_objects(
        self,
        prefix: str,
        *,
        delimiter: str | None = None,
        max_keys: int = 1000,
        continuation_token: str | None = None,
    ) -> dict:
        """List objects and common prefixes for a given prefix."""
        await self._ensure_client()
        params: dict[str, object] = {
            "Bucket": settings.s3_bucket,
            "Prefix": prefix,
            "MaxKeys": max_keys,
        }
        if delimiter:
            params["Delimiter"] = delimiter
        if continuation_token:
            params["ContinuationToken"] = continuation_token

        response = await self._s3.list_objects_v2(**params)
        contents = response.get("Contents", [])
        common_prefixes = response.get("CommonPrefixes", [])
        return {
            "objects": [
                {
                    "key": obj.get("Key"),
                    "size": obj.get("Size"),
                    "last_modified": obj.get("LastModified"),
                }
                for obj in contents
            ],
            "common_prefixes": [
                prefix_obj.get("Prefix") for prefix_obj in common_prefixes
            ],
            "is_truncated": response.get("IsTruncated", False),
            "next_token": response.get("NextContinuationToken"),
        }

    async def get_presigned_url(self, s3_key: str, expiration: int = 3600) -> str:
        """
        Generate a presigned URL for accessing an S3 object.

        Args:
            s3_key: S3 key
            expiration: URL expiration time in seconds (default 1 hour)

        Returns:
            Presigned URL
        """
        await self._ensure_client()
        url: str = await self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": s3_key},
            ExpiresIn=expiration,
        )
        return url

    async def get_presigned_upload_url(
        self,
        s3_key: str,
        *,
        expiration: int = 3600,
        content_type: str | None = None,
    ) -> str:
        """Generate a presigned URL for uploading an S3 object with PUT."""
        await self._ensure_client()
        params: dict[str, str] = {"Bucket": settings.s3_bucket, "Key": s3_key}
        if content_type:
            params["ContentType"] = content_type
        url: str = await self._s3.generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=expiration,
            HttpMethod="PUT",
        )
        return url

    async def get_presigned_urls_batch(
        self, s3_keys: list[str], expiration: int = 3600
    ) -> dict[str, str]:
        """
        Generate presigned URLs for multiple S3 objects in parallel.

        Args:
            s3_keys: List of S3 keys
            expiration: URL expiration time in seconds (default 1 hour)

        Returns:
            Dict mapping s3_key -> presigned URL
        """
        await self._ensure_client()
        import asyncio

        async def generate_url(key: str) -> tuple[str, str]:
            url: str = await self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.s3_bucket, "Key": key},
                ExpiresIn=expiration,
            )
            return (key, url)

        results = await asyncio.gather(*[generate_url(key) for key in s3_keys])
        return dict(results)


# Global storage client instance
_storage_client: StorageClient | None = None


def get_storage_client() -> StorageClient:
    """Get the global storage client instance."""
    global _storage_client
    if _storage_client is None:
        _storage_client = StorageClient()
    return _storage_client


def normalize_s3_prefix(prefix: str | None) -> str | None:
    """Normalize an S3 prefix and keep it directory-shaped."""
    if not prefix:
        return None
    normalized = normalize_s3_relative_path(prefix).rstrip("/")
    if not normalized:
        return None
    return f"{normalized}/"


def resolve_s3_key(task_s3_key: str | None, task_path: str | None) -> str | None:
    """Resolve an S3 key from a stored key or s3:// path."""
    return task_s3_key or extract_s3_key_from_path(task_path)


def resolve_trial_s3_prefix(
    trial_id: str,
    *,
    trial_s3_key: str | None,
    trial_result_path: str | None = None,
) -> str:
    """Resolve a trial S3 prefix, falling back to the default trial layout."""
    resolved = resolve_s3_key(trial_s3_key, trial_result_path)
    return normalize_s3_prefix(resolved or StorageClient._trial_prefix(trial_id)) or (
        StorageClient._trial_prefix(trial_id)
    )


def collect_s3_prefixes_for_deletion(
    *,
    tasks: list[tuple[str | None, str | None]],
    trials: list[tuple[str, str | None]],
) -> list[str]:
    """Collect unique task and trial S3 prefixes that should be deleted."""
    prefixes: list[str] = []

    for task_s3_key, task_path in tasks:
        resolved = normalize_s3_prefix(resolve_s3_key(task_s3_key, task_path))
        if resolved:
            prefixes.append(resolved)

    for trial_id, trial_s3_key in trials:
        prefixes.append(resolve_trial_s3_prefix(trial_id, trial_s3_key=trial_s3_key))

    return list(dict.fromkeys(prefixes))


async def delete_s3_prefixes(prefixes: list[str]) -> int:
    """Delete S3 objects for the provided prefixes."""
    normalized: list[str] = []
    for prefix in prefixes:
        resolved = normalize_s3_prefix(prefix)
        if resolved:
            normalized.append(resolved)
    normalized = list(dict.fromkeys(normalized))
    if not normalized:
        return 0
    storage = get_storage_client()
    return await storage.delete_prefixes(normalized)


def resolve_mounted_task_directory(task_s3_key: str | None) -> Path | None:
    """Return a mounted task path when worker bucket mounts are configured."""
    if not task_s3_key:
        return None

    key_prefix = normalize_s3_relative_path(WORKER_TASK_KEY_PREFIX)
    normalized_key = normalize_s3_relative_path(task_s3_key).rstrip("/")
    if not key_prefix:
        return None

    normalized_prefix = f"{key_prefix.rstrip('/')}/"
    if not normalized_key.startswith(normalized_prefix):
        return None

    relative_path = normalized_key[len(normalized_prefix) :]
    if not relative_path:
        return None

    candidate = WORKER_TASK_MOUNT_PATH / relative_path
    if candidate.exists() and (candidate / "task.toml").exists():
        return candidate
    return None


async def resolve_task_directory(
    task_id: str,
    *,
    task_s3_key: str | None,
    task_path: str | None,
) -> tuple[Path, Path | None, str | None]:
    """
    Resolve a task directory from S3 or local storage.

    Returns: (task_dir_to_use, temp_dir_to_cleanup, resolved_s3_key)
    """
    resolved_s3_key = resolve_s3_key(task_s3_key, task_path)
    if resolved_s3_key:
        mounted_task_path = resolve_mounted_task_directory(resolved_s3_key)
        if mounted_task_path is not None:
            return mounted_task_path, None, resolved_s3_key

        storage = get_storage_client()
        temp_dir = Path(tempfile.mkdtemp(prefix=f"task-{task_id}-"))
        try:
            await storage.download_task_directory(resolved_s3_key, temp_dir)
            return temp_dir, temp_dir, resolved_s3_key
        except Exception as exc:
            _cleanup_temp_directory(temp_dir)
            if task_path:
                local_task_path = Path(task_path)
                if local_task_path.exists():
                    return local_task_path, None, resolved_s3_key
            raise ValueError(
                f"Failed to download task from S3 and no local path available: {exc}"
            ) from exc

    if not task_path:
        raise ValueError(
            "No task location available - neither S3 key nor local path is set."
        )

    local_task_path = Path(task_path)
    if not local_task_path.exists():
        raise ValueError(f"Task path does not exist: {task_path}")
    return local_task_path, None, None


async def resolve_trial_directory(
    trial_id: str,
    *,
    trial_s3_key: str | None,
    trial_result_path: str | None,
) -> tuple[Path, Path | None, str | None]:
    """
    Resolve a trial directory from S3 or local storage.

    Returns: (trial_dir_to_use, temp_dir_to_cleanup, resolved_s3_key)
    """
    resolved_s3_key = resolve_s3_key(trial_s3_key, trial_result_path)
    if resolved_s3_key:
        storage = get_storage_client()
        temp_dir = Path(tempfile.mkdtemp(prefix=f"trial-{trial_id}-"))
        try:
            await storage.download_trial_directory(resolved_s3_key, temp_dir)
            return temp_dir, temp_dir, resolved_s3_key
        except Exception as exc:
            _cleanup_temp_directory(temp_dir)
            if trial_result_path:
                local_trial_path = Path(trial_result_path)
                if local_trial_path.exists():
                    return local_trial_path, None, resolved_s3_key
            raise ValueError(
                f"Failed to download trial from S3 and no local path available: {exc}"
            ) from exc

    if not trial_result_path:
        raise ValueError(
            "No trial location available - neither S3 key nor local result path is set."
        )

    local_trial_path = Path(trial_result_path)
    if not local_trial_path.exists():
        raise ValueError(f"Trial result path does not exist: {trial_result_path}")
    return local_trial_path, None, None

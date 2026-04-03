from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import json
import posixpath
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import aioboto3
from botocore.exceptions import ClientError
from fastapi import HTTPException
from oddish.config import settings

WORKER_TASK_MOUNT_PATH = Path("/mnt/oddish-tasks")
WORKER_TASK_KEY_PREFIX = "tasks/"


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
                "last_modified": datetime.fromtimestamp(
                    member.mtime, tz=timezone.utc
                ),
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

    raise HTTPException(status_code=404, detail=f"Task file not found: {normalized_path}")


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

    async def upload_task_archive(self, task_id: str, archive_path: Path) -> str:
        """Upload a task tarball as a single S3 object."""
        await self._ensure_client()
        if not archive_path.exists() or not archive_path.is_file():
            raise ValueError(f"Task archive does not exist: {archive_path}")

        archive_key = self._task_archive_key(task_id)
        await self.upload_file(archive_path, archive_key)
        return f"tasks/{task_id}/"

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

        # List all objects with this prefix
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
    ) -> dict:
        """List files in a task's S3 directory."""
        root_prefix = f"tasks/{task_id}/"
        archive_key = self._task_archive_key(task_id)
        if await self.object_exists(archive_key):
            archive_bytes = await self.download_bytes(archive_key)
            archive_files = _task_archive_members_from_bytes(archive_bytes)
            relative_prefix = normalize_s3_relative_path(prefix)
            if relative_prefix and not relative_prefix.endswith("/"):
                relative_prefix = f"{relative_prefix}/"
            full_prefix = f"{root_prefix}{relative_prefix}"

            filtered_files = [
                file_meta
                for file_meta in archive_files
                if not relative_prefix or str(file_meta["path"]).startswith(relative_prefix)
            ]
            if recursive:
                return {
                    "task_id": task_id,
                    "files": filtered_files,
                    "dirs": [],
                    "prefix": full_prefix,
                    "recursive": True,
                    "presigned": False,
                    "presign_expires_in": None,
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
                "presigned": False,
                "presign_expires_in": None,
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
                s3_keys = [f["key"] for f in files]
                urls = await self.get_presigned_urls_batch(s3_keys, presign_expiration)
                for f in files:
                    f["url"] = urls.get(f["key"])

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
            s3_keys = [f["key"] for f in files]
            urls = await self.get_presigned_urls_batch(s3_keys, presign_expiration)
            for f in files:
                f["url"] = urls.get(f["key"])

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

    async def get_task_file_content(
        self,
        *,
        task_id: str,
        file_path: str,
        presign: bool,
        presign_expiration: int = 900,
    ) -> dict:
        """Get content of a specific task file from S3."""
        normalized_path = normalize_s3_relative_path(file_path)
        if not normalized_path:
            raise HTTPException(status_code=400, detail="Invalid file path")
        archive_key = self._task_archive_key(task_id)
        if await self.object_exists(archive_key):
            archive_bytes = await self.download_bytes(archive_key)
            content = _read_task_archive_text(archive_bytes, normalized_path)
            return {
                "path": normalized_path,
                "content": content,
                "key": f"{archive_key}#{normalized_path}",
            }
        s3_key = f"tasks/{task_id}/{normalized_path}"

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
    """Delete S3 objects for the provided prefixes when S3 is enabled."""
    normalized: list[str] = []
    for prefix in prefixes:
        resolved = normalize_s3_prefix(prefix)
        if resolved:
            normalized.append(resolved)
    normalized = list(dict.fromkeys(normalized))
    if not normalized or not settings.s3_enabled:
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

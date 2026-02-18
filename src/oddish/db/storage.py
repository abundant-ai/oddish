from __future__ import annotations

import json
import posixpath
import tempfile
from pathlib import Path, PurePosixPath

import aioboto3
from fastapi import HTTPException
from oddish.config import settings


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


class StorageClient:
    """
    Async S3-compatible storage client.

    Supports AWS S3, Supabase Storage, MinIO, and any S3-compatible backend.
    """

    def __init__(self):
        self._client: aioboto3.Client | None = None
        self._session: aioboto3.Session | None = None

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

        # Upload all files in the directory recursively
        for file_path in local_path.rglob("*"):
            if file_path.is_file():
                relative_path = file_path.relative_to(local_path)
                s3_key = f"{s3_prefix}{relative_path}"
                await self.upload_file(file_path, s3_key)

        return s3_prefix

    async def download_task_directory(self, s3_prefix: str, local_path: Path) -> None:
        """
        Download a task directory from S3.

        Args:
            s3_prefix: S3 key prefix (e.g., "tasks/abc123/")
            local_path: Local path where to download the task
        """
        await self._ensure_client()
        local_path.mkdir(parents=True, exist_ok=True)

        # List all objects with this prefix
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=settings.s3_bucket, Prefix=s3_prefix
        ):
            for obj in page.get("Contents", []):
                s3_key = obj["Key"]
                # Calculate relative path
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

        # Upload all files
        for file_path in harbor_job_dir.rglob("*"):
            if file_path.is_file():
                relative_path = file_path.relative_to(harbor_job_dir)
                s3_key = f"{s3_prefix}{relative_path}"
                await self.upload_file(file_path, s3_key)

        return s3_prefix

    async def download_trial_directory(self, s3_prefix: str, local_path: Path) -> None:
        """
        Download a trial directory from S3.

        Args:
            s3_prefix: S3 key prefix for the trial (e.g., "tasks/abc123/trials/abc123-0/")
            local_path: Local path where to download the trial results
        """
        await self._ensure_client()
        local_path.mkdir(parents=True, exist_ok=True)

        # List all objects with this prefix
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=settings.s3_bucket, Prefix=s3_prefix
        ):
            for obj in page.get("Contents", []):
                s3_key = obj["Key"]
                # Calculate relative path
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

        # List all log files
        paginator = self._client.get_paginator("list_objects_v2")
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
            await self._client.put_object(
                Bucket=settings.s3_bucket,
                Key=s3_key,
                Body=f.read(),
            )

    async def download_file(self, s3_key: str, local_path: Path) -> None:
        """Download a file from S3."""
        await self._ensure_client()
        response = await self._client.get_object(
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
        response = await self._client.get_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
        )
        async with response["Body"] as stream:
            content = await stream.read()
            return content

    async def download_text(self, s3_key: str) -> str:
        """Download text content from S3."""
        await self._ensure_client()
        response = await self._client.get_object(
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
        response = await self._client.get_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
        )
        async with response["Body"] as stream:
            content = await stream.read()
            result: dict = json.loads(content.decode("utf-8"))
            return result

    async def list_keys(self, prefix: str) -> list[str]:
        """List all keys with a given prefix."""
        await self._ensure_client()
        keys = []
        paginator = self._client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    async def list_objects_all(self, prefix: str) -> list[dict]:
        """List all objects with metadata (key, size, last_modified) for a given prefix."""
        await self._ensure_client()
        objects = []
        paginator = self._client.get_paginator("list_objects_v2")
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

        response = await self._client.list_objects_v2(**params)
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
        url: str = await self._client.generate_presigned_url(
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
            url: str = await self._client.generate_presigned_url(
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


def resolve_s3_key(task_s3_key: str | None, task_path: str | None) -> str | None:
    """Resolve an S3 key from a stored key or s3:// path."""
    return task_s3_key or extract_s3_key_from_path(task_path)


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
                "Failed to download trial from S3 and no local path available: "
                f"{exc}"
            ) from exc

    if not trial_result_path:
        raise ValueError(
            "No trial location available - neither S3 key nor local result path is set."
        )

    local_trial_path = Path(trial_result_path)
    if not local_trial_path.exists():
        raise ValueError(f"Trial result path does not exist: {trial_result_path}")
    return local_trial_path, None, None

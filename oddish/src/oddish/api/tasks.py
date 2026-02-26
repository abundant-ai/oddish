from __future__ import annotations

import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from oddish.config import settings
from oddish.db.storage import get_storage_client
from oddish.schemas import UploadResponse


async def _write_upload_to_file(
    file: UploadFile, destination: Path, *, max_bytes: int
) -> int:
    total = 0
    chunk_size = 1024 * 1024
    with destination.open("wb") as handle:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes and total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Task upload too large. Max size is {settings.max_task_upload_mb}MB."
                    ),
                )
            handle.write(chunk)
    return total


async def handle_task_upload(file: UploadFile) -> UploadResponse:
    """Upload a task tarball to S3 or local storage."""
    original_filename = file.filename or "task.tar.gz"
    name_stem = Path(original_filename).stem
    if name_stem.endswith(".tar"):
        name_stem = Path(name_stem).stem

    # name_stem is the human-readable task name
    task_name = name_stem
    task_id = f"{name_stem}-{str(uuid.uuid4())[:8]}"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        task_dir = tmpdir_path / "task"
        task_dir.mkdir()

        tarball_path = tmpdir_path / "task.tar.gz"
        max_bytes = max(settings.max_task_upload_mb, 0) * 1024 * 1024
        await _write_upload_to_file(file, tarball_path, max_bytes=max_bytes)

        try:
            with tarfile.open(tarball_path, "r:gz") as tar:
                members = tar.getmembers()
                for member in members:
                    if member.islnk() or member.issym():
                        raise HTTPException(
                            status_code=400, detail="Invalid tarball: links not allowed"
                        )
                    member_path = Path(member.name)
                    if member_path.is_absolute():
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid tarball: absolute paths not allowed",
                        )
                    resolved = (task_dir / member.name).resolve()
                    if task_dir not in resolved.parents and resolved != task_dir:
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid tarball: path traversal",
                        )
                for member in members:
                    tar.extract(member, path=task_dir)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid tarball: {str(e)}")

        if settings.s3_enabled:
            storage = get_storage_client()
            try:
                s3_key = await storage.upload_task_directory(task_id, task_dir)
                return UploadResponse(task_id=task_id, name=task_name, s3_key=s3_key)
            except Exception as e:
                raise HTTPException(
                    status_code=500, detail=f"Failed to upload to S3: {str(e)}"
                )

        local_storage = Path(settings.local_storage_dir)
        local_storage.mkdir(parents=True, exist_ok=True)
        task_storage_path = local_storage / task_id
        shutil.copytree(task_dir, task_storage_path)
        return UploadResponse(
            task_id=task_id, name=task_name, task_path=str(task_storage_path)
        )


async def resolve_task_storage(
    task_id: str,
    *,
    s3_missing_detail: str | None = None,
    local_missing_detail: str | None = None,
) -> tuple[str, str | None]:
    """Resolve task path based on storage mode, verifying existence."""
    if settings.s3_enabled:
        task_s3_key = f"tasks/{task_id}/"
        storage = get_storage_client()
        try:
            keys = await storage.list_keys(task_s3_key)
            if not keys:
                raise HTTPException(
                    status_code=404,
                    detail=s3_missing_detail or f"Task {task_id} not found in S3",
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to check S3: {str(e)}")

        return f"s3://{task_s3_key}", task_s3_key

    local_storage = Path(settings.local_storage_dir) / task_id
    if not local_storage.exists():
        raise HTTPException(
            status_code=404,
            detail=local_missing_detail or f"Task {task_id} not found",
        )

    return str(local_storage), None

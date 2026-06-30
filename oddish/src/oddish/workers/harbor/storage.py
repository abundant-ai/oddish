from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

_MIN_REQUIRED_FREE_GB = 5.0
_MIN_REQUIRED_FREE_INODES = 1024


def _storage_probe_paths(jobs_dir: Path, *, include_temp_root: bool) -> list[Path]:
    """Return the local scratch roots Oddish should verify before Harbor runs."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    raw_paths: tuple[Path, ...] = (jobs_dir,)
    if include_temp_root:
        raw_paths = (jobs_dir, Path(tempfile.gettempdir()))
    for raw_path in raw_paths:
        resolved = raw_path.resolve()
        if resolved in seen:
            continue
        candidates.append(resolved)
        seen.add(resolved)
    return candidates


def _probe_storage_root(
    path: Path,
    *,
    min_required_gb: float,
    min_required_inodes: int,
) -> str | None:
    """Check bytes, inode headroom, and writeability for one local root."""
    path.mkdir(parents=True, exist_ok=True)

    disk_usage = shutil.disk_usage(path)
    free_gb = disk_usage.free / (1024**3)
    if free_gb < min_required_gb:
        return (
            f"Insufficient local storage at {path}: {free_gb:.1f}GB free "
            f"(minimum {min_required_gb:.1f}GB required)"
        )

    statvfs = os.statvfs(path)
    # Filesystems that don't expose an inode table (overlayfs, btrfs, many
    # tmpfs mounts, Modal's ephemeral "/tmp") report f_files == 0, which forces
    # f_ffree == f_favail == 0 too. That is the "unlimited inodes" signal, not
    # "0 free", so skip the inode check entirely when there is no table.
    total_inodes = getattr(statvfs, "f_files", None)
    if total_inodes:
        free_inodes = getattr(statvfs, "f_favail", None)
        if free_inodes is None or free_inodes < 0:
            free_inodes = getattr(statvfs, "f_ffree", None)
        if free_inodes is not None and free_inodes < min_required_inodes:
            return (
                f"Insufficient local storage inodes at {path}: {free_inodes} free "
                f"(minimum {min_required_inodes} required)"
            )

    probe_dir = path / f".oddish-preflight-{uuid.uuid4().hex}"
    probe_file = probe_dir / "probe.txt"
    try:
        probe_dir.mkdir()
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        probe_dir.rmdir()
    except OSError as exc:
        shutil.rmtree(probe_dir, ignore_errors=True)
        return f"Local storage probe failed at {path}: {type(exc).__name__}: {exc}"
    return None


def log_local_storage_snapshot(path: str | Path) -> None:
    """Log a one-line disk + inode snapshot for *path* on startup.

    Captured once per process start (API server, standalone worker, Modal
    container) so operators can tell at a glance whether a given container
    is on an inode-tracking filesystem (ext4 shows ``N/M inodes free``)
    versus one that doesn't (overlayfs/tmpfs on Modal shows
    ``inode table unlimited``). Never raises -- a startup log line should
    not block the process from coming up.
    """
    try:
        probe_path = Path(path)
        probe_path.mkdir(parents=True, exist_ok=True)
        disk_usage = shutil.disk_usage(probe_path)
        statvfs = os.statvfs(probe_path)
        free_gb = disk_usage.free / (1024**3)
        total_gb = disk_usage.total / (1024**3)
        total_inodes = getattr(statvfs, "f_files", 0) or 0
        free_inodes = getattr(statvfs, "f_favail", None)
        if free_inodes is None or free_inodes < 0:
            free_inodes = getattr(statvfs, "f_ffree", 0) or 0
        if total_inodes:
            inode_desc = f"{free_inodes}/{total_inodes} inodes free"
        else:
            inode_desc = "inode table unlimited (no tracking)"
        print(
            f"[oddish] storage snapshot at {probe_path}: "
            f"{free_gb:.1f}GB/{total_gb:.1f}GB bytes free, {inode_desc}",
            flush=True,
        )
    except Exception as exc:
        print(
            f"[oddish] storage snapshot at {path} failed: {type(exc).__name__}: {exc}",
            flush=True,
        )


def _check_local_storage_preflight(
    jobs_dir: Path,
    *,
    include_temp_root: bool,
    min_required_gb: float = _MIN_REQUIRED_FREE_GB,
    min_required_inodes: int = _MIN_REQUIRED_FREE_INODES,
) -> str | None:
    """Return a user-facing error when Harbor scratch space is not viable."""
    for root in _storage_probe_paths(jobs_dir, include_temp_root=include_temp_root):
        try:
            error = _probe_storage_root(
                root,
                min_required_gb=min_required_gb,
                min_required_inodes=min_required_inodes,
            )
        except OSError as exc:
            return (
                f"Local storage preflight failed at {root}: {type(exc).__name__}: {exc}"
            )
        if error is not None:
            return error
    return None

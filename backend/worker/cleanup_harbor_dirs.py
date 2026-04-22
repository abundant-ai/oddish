"""One-shot cleanup for leaked Harbor per-trial wrapper directories.

Pre-PR-#14, a successful trial upload removed the nested Harbor job
directory but left the outer wrapper (pattern
``{task_temp}.{agent}.{task_id}-{idx}``) behind under ``/data/harbor``.
Over time these empty wrappers exhausted the Modal volume's inodes, so
Harbor's ``unique_parent.mkdir(...)`` started raising
``OSError: [Errno 28] No space left on device`` even when bytes-free
looked fine. PR #14 prunes these on upload going forward but doesn't
sweep up historical leaks. This module is that sweeper.

This script is intentionally standalone: it declares its own tiny
Modal app with a ``debian_slim`` image and mounts the live Oddish
volume by name (same ``MODAL_VOLUME_NAME`` convention
``backend/modal_app.py`` uses). That keeps cold-start tiny compared to
importing the full backend image + Oddish deps.

Invoke from the repo root (works from anywhere — no ``cd backend``
needed):

    modal run backend/worker/cleanup_harbor_dirs.py                 # dry-run
    modal run backend/worker/cleanup_harbor_dirs.py --no-dry-run    # commit

Flags:

    --min-age-hours 13   Only remove wrappers whose mtime and every
                         direct child's mtime are older than this
                         cutoff. Default 13h (slightly above
                         ``WORKER_TIMEOUT_SECONDS`` = 12h) so an
                         in-flight long-running trial is never touched.
    --dry-run / --no-dry-run
                         Default ``True`` — preview first, then flip
                         to ``--no-dry-run`` once the preview numbers
                         look right.
    --pattern 'task-*'   Glob matched against direct children of the
                         Harbor jobs dir. Default matches Harbor's
                         per-trial wrapper name.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from time import time

import modal


_VOLUME_NAME = os.environ.get("MODAL_VOLUME_NAME", "oddish")
_VOLUME_MOUNT_PATH = "/data"
_HARBOR_JOBS_DIR = Path(f"{_VOLUME_MOUNT_PATH}/harbor")
_SECONDS_PER_HOUR = 3600


app = modal.App("oddish-harbor-cleanup")
image = modal.Image.debian_slim(python_version="3.12")
volume = modal.Volume.from_name(_VOLUME_NAME, create_if_missing=False)


@dataclass
class _StorageStats:
    free_bytes: int
    total_bytes: int
    free_inodes: int | None
    entry_count: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / (1024**3)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024**3)


def _collect_stats(path: Path) -> _StorageStats:
    """Snapshot bytes, inodes, and immediate-child count for ``path``."""
    usage = shutil.disk_usage(path)
    try:
        vfs = os.statvfs(path)
        free_inodes: int | None = getattr(vfs, "f_favail", None)
        if free_inodes is None or free_inodes < 0:
            free_inodes = getattr(vfs, "f_ffree", None)
    except OSError:
        free_inodes = None
    entry_count = sum(1 for _ in path.iterdir()) if path.exists() else 0
    return _StorageStats(
        free_bytes=usage.free,
        total_bytes=usage.total,
        free_inodes=free_inodes,
        entry_count=entry_count,
    )


def _format_stats(stats: _StorageStats) -> str:
    inodes = (
        f"{stats.free_inodes:,}" if stats.free_inodes is not None else "n/a"
    )
    return (
        f"free={stats.free_gb:.1f}GB/{stats.total_gb:.1f}GB "
        f"inodes_free={inodes} children={stats.entry_count:,}"
    )


@app.function(
    image=image,
    volumes={_VOLUME_MOUNT_PATH: volume},
    timeout=3600,
    memory=512,
)
def cleanup_harbor_leaked_wrappers(
    min_age_hours: float = 13.0,
    dry_run: bool = True,
    pattern: str = "task-*",
) -> dict:
    """Remove stale Harbor wrapper directories under ``/data/harbor``.

    A wrapper directory is eligible for removal iff:

    - its name matches ``pattern`` (default ``"task-*"``), AND
    - its own mtime is older than ``min_age_hours`` hours, AND
    - every direct child's mtime is also older than ``min_age_hours``.

    The "any direct child touched recently" guard covers the case where
    Harbor adds subdirectories to the wrapper shortly after creating
    it — the outer dir's mtime only updates on direct add/remove, not
    on nested writes, so we double-check one level deep.
    """
    print(
        f"[cleanup] harbor_jobs_dir={_HARBOR_JOBS_DIR} "
        f"min_age_hours={min_age_hours} dry_run={dry_run} pattern={pattern}"
    )

    # See the latest state across all worker containers before we start.
    volume.reload()

    if not _HARBOR_JOBS_DIR.exists():
        print(f"[cleanup] {_HARBOR_JOBS_DIR} does not exist; nothing to do")
        return {"removed": 0, "skipped_recent": 0, "errors": 0}

    before = _collect_stats(_HARBOR_JOBS_DIR)
    print(f"[cleanup] before: {_format_stats(before)}")

    now = time()
    age_cutoff = now - min_age_hours * _SECONDS_PER_HOUR

    removed = 0
    skipped_recent = 0
    skipped_nonmatch = 0
    errors = 0

    for entry in _HARBOR_JOBS_DIR.iterdir():
        if not entry.is_dir():
            continue
        if not entry.match(pattern):
            skipped_nonmatch += 1
            continue

        try:
            entry_mtime = entry.stat().st_mtime
        except FileNotFoundError:
            # Raced with another cleanup / cycling container.
            continue
        except OSError as exc:
            print(f"[cleanup] stat failed for {entry.name}: {exc}")
            errors += 1
            continue

        if entry_mtime > age_cutoff:
            skipped_recent += 1
            continue

        # Double-check one level deep for recent writes. See docstring.
        any_child_recent = False
        try:
            for child in entry.iterdir():
                try:
                    if child.stat().st_mtime > age_cutoff:
                        any_child_recent = True
                        break
                except OSError:
                    continue
        except OSError:
            # Dir vanished; treat as already-cleaned.
            continue
        if any_child_recent:
            skipped_recent += 1
            continue

        if dry_run:
            if removed < 10:
                print(f"[cleanup] DRY-RUN would remove {entry.name}")
            removed += 1
            continue

        try:
            shutil.rmtree(entry, ignore_errors=False)
            removed += 1
            if removed % 200 == 0:
                print(f"[cleanup] removed {removed} so far ...")
        except Exception as exc:
            print(f"[cleanup] rmtree failed for {entry.name}: {exc}")
            errors += 1

    if not dry_run and removed > 0:
        # Commit the deletes so other containers see the reclaim.
        volume.commit()

    after = _collect_stats(_HARBOR_JOBS_DIR)
    print(f"[cleanup] after:  {_format_stats(after)}")
    bytes_freed_est = max(after.free_bytes - before.free_bytes, 0)
    print(
        f"[cleanup] done: removed={removed} skipped_recent={skipped_recent} "
        f"skipped_nonmatch={skipped_nonmatch} errors={errors} "
        f"freed_gb_est={bytes_freed_est / (1024**3):.2f}"
    )
    return {
        "removed": removed,
        "skipped_recent": skipped_recent,
        "skipped_nonmatch": skipped_nonmatch,
        "errors": errors,
        "before": {
            "free_gb": round(before.free_gb, 2),
            "free_inodes": before.free_inodes,
            "children": before.entry_count,
        },
        "after": {
            "free_gb": round(after.free_gb, 2),
            "free_inodes": after.free_inodes,
            "children": after.entry_count,
        },
        "dry_run": dry_run,
    }


@app.local_entrypoint()
def main(
    min_age_hours: float = 13.0,
    dry_run: bool = True,
    pattern: str = "task-*",
) -> None:
    """Invoke with ``modal run backend/worker/cleanup_harbor_dirs.py``."""
    result = cleanup_harbor_leaked_wrappers.remote(
        min_age_hours=min_age_hours,
        dry_run=dry_run,
        pattern=pattern,
    )
    print(result)

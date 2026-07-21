"""Backfill task-registry metadata for tasks uploaded before the schema-ingest
feature landed.

Metadata (category/description/author/resources/timeouts) is now captured at
UPLOAD time (``complete_task_upload`` in ``oddish/core/tasks.py``), but every
task that already existed when the feature shipped has ``metadata_source =
NULL`` and no derived tags. This script re-derives that metadata for existing
tasks by reading ``task.toml`` back out of the per-file S3 layout the
``TASK_EXPAND`` worker already materializes (``tasks/{task_id}/v{N}-files/``,
see ``oddish/workers/queue/task_expand_handler.py``), projecting it through
the SAME pure function the upload path uses
(``oddish.core.task_metadata.project_task_config``), and writing the columns
+ derived tags exactly like a fresh upload would.

Mirrors ``backfill_analysis.py``'s CLI shape: dry-run is the default, pass
``--execute`` to actually write.

Idempotent by refresh, not by no-op: a task whose ``metadata_source`` is
already BACKFILL is recomputed from the same ``task.toml`` every run, so it
converges to the same columns/tags each time (``rebuild_derived_tags``
replaces the derived tag set rather than accumulating it, and the
``ci_pr_number`` upload-event write is deduped explicitly below). Only
``metadata_source = CLIENT`` -- a real upload -- is treated as authoritative
and skipped, unless ``--force``.

Usage:
    cd backend
    uv run python scripts/backfill_task_metadata.py --task-names foo,bar        # dry-run
    uv run python scripts/backfill_task_metadata.py --task-names foo,bar --execute
    uv run python scripts/backfill_task_metadata.py --all --execute
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import tomllib
from dataclasses import dataclass, field

import pydantic
from harbor.models.task.config import TaskConfig
from sqlalchemy import select

from oddish.core.helpers import _parse_github_meta
from oddish.core.tags.derived import DERIVED_TAG_ERRORS, rebuild_derived_tags
from oddish.core.task_metadata import project_task_config
from oddish.db.connection import async_session_maker
from oddish.db.models import (
    MetadataSource,
    TaskModel,
    TaskUploadEventModel,
    TaskVersionModel,
    utcnow,
)
from oddish.db.storage import StorageClient, get_storage_client

logger = logging.getLogger("backfill_task_metadata")

# Marks a task_upload_events row as written by this script rather than a real
# upload, so it's trivially distinguishable in queries/admin views (no real
# uploader ever reports a CLI "version" that isn't a version number).
BACKFILL_UPLOADER_MARKER = "oddish-backfill-task-metadata"


@dataclass
class TaskOutcome:
    task_id: str
    status: str  # "backfilled" | "skipped" | "failed"
    reason: str | None = None
    category: str | None = None


@dataclass
class BackfillSummary:
    examined: int = 0
    backfilled: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def record(self, outcome: TaskOutcome) -> None:
        self.examined += 1
        if outcome.status == "backfilled":
            self.backfilled.append(outcome.task_id)
        elif outcome.status == "skipped":
            self.skipped.append((outcome.task_id, outcome.reason or "unknown"))
        else:
            self.failed.append((outcome.task_id, outcome.reason or "unknown"))

    def print_report(self) -> None:
        print(
            f"\n=== examined={self.examined} backfilled={len(self.backfilled)} "
            f"skipped={len(self.skipped)} failed={len(self.failed)} ==="
        )
        if self.skipped:
            print(f"\n{len(self.skipped)} skipped:")
            for task_id, reason in self.skipped:
                print(f"  SKIP  {task_id}: {reason}")
        if self.failed:
            print(f"\n{len(self.failed)} failed:")
            for task_id, reason in self.failed:
                print(f"  FAIL  {task_id}: {reason}")


def _expanded_prefix_for(task_id: str, version: int) -> str:
    """Mirrors ``task_expand_handler._expanded_prefix_for`` exactly."""
    return f"tasks/{task_id}/v{version}-files/"


def _matches_task_name(task_id: str, task_name: str, selector: str) -> bool:
    """A selector matches its exact task id/name, or a bare name matches
    every "{name}-{random suffix}" id (mirrors backfill_analysis.py batch())."""
    if task_id == selector or task_name == selector:
        return True
    return bool(re.match(rf"^{re.escape(selector)}-[0-9a-f]{{6,}}$", task_id))


async def _select_task_ids(session, *, task_names: list[str] | None) -> list[str]:
    rows = (await session.execute(select(TaskModel.id, TaskModel.name))).all()
    if not task_names:
        return sorted(r.id for r in rows)
    wanted: set[str] = set()
    for selector in task_names:
        wanted.update(r.id for r in rows if _matches_task_name(r.id, r.name, selector))
    return sorted(wanted)


async def _maybe_recover_ci_pr_number(
    session, *, task: TaskModel, version: TaskVersionModel
) -> None:
    """Recover ``ci_pr_number`` from ``tasks.tags->>'github_meta'`` where
    present. Every other provenance field is genuinely unrecoverable for a
    historical upload (git context was never captured), so this is the only
    ``task_upload_events`` field this script ever populates.
    """
    meta = _parse_github_meta(task.tags)
    if not meta:
        return
    raw_pr_number = meta.get("pr_number")
    if not raw_pr_number:
        return
    try:
        pr_number = int(raw_pr_number)
    except (TypeError, ValueError):
        return

    # Explicit dedupe (independent of tasks.py's time-windowed retry dedupe)
    # so re-running this script years apart never creates a second row for
    # the same recovered PR number.
    existing = await session.scalar(
        select(TaskUploadEventModel.id).where(
            TaskUploadEventModel.task_id == task.id,
            TaskUploadEventModel.uploader_cli_version == BACKFILL_UPLOADER_MARKER,
            TaskUploadEventModel.ci_pr_number == pr_number,
        )
    )
    if existing is not None:
        return

    session.add(
        TaskUploadEventModel(
            task_id=task.id,
            task_version_id=version.id,
            created_version=False,
            content_hash=version.content_hash,
            ci_pr_number=pr_number,
            uploader_cli_version=BACKFILL_UPLOADER_MARKER,
        )
    )


async def _backfill_one_task(
    session,
    task_id: str,
    *,
    force: bool,
    storage: StorageClient,
) -> TaskOutcome:
    task = await session.get(TaskModel, task_id)
    if task is None:
        return TaskOutcome(task_id, "skipped", "task not found (deleted?)")

    if task.metadata_source == MetadataSource.CLIENT and not force:
        return TaskOutcome(
            task_id,
            "skipped",
            "metadata_source=CLIENT is authoritative (pass --force to override)",
        )

    if task.current_version_id is None:
        return TaskOutcome(task_id, "skipped", "task has no current_version_id")
    version = await session.get(TaskVersionModel, task.current_version_id)
    if version is None:
        return TaskOutcome(task_id, "skipped", "current_version row is missing")

    prefix = _expanded_prefix_for(task.id, version.version)
    manifest_key = f"{prefix}{StorageClient._EXPANDED_MANIFEST_OBJECT_NAME}"
    try:
        manifest_exists = await storage.object_exists(manifest_key)
    except Exception as exc:
        return TaskOutcome(
            task_id,
            "skipped",
            f"S3 error checking manifest: {type(exc).__name__}: {exc}",
        )
    if not manifest_exists:
        return TaskOutcome(
            task_id,
            "skipped",
            f"no expanded files at {prefix} (never expanded, or expansion failed)",
        )

    toml_key = f"{prefix}task.toml"
    try:
        toml_exists = await storage.object_exists(toml_key)
    except Exception as exc:
        return TaskOutcome(
            task_id,
            "skipped",
            f"S3 error checking task.toml: {type(exc).__name__}: {exc}",
        )
    if not toml_exists:
        return TaskOutcome(task_id, "skipped", "task.toml missing from expanded files")

    try:
        raw = await storage.download_bytes(toml_key)
        text = raw.decode("utf-8")
        config = TaskConfig.model_validate_toml(text)
        config_dict = config.model_dump(mode="json", exclude_none=True)
    except (
        tomllib.TOMLDecodeError,
        UnicodeDecodeError,
        pydantic.ValidationError,
    ) as exc:
        return TaskOutcome(
            task_id, "skipped", f"malformed task.toml: {type(exc).__name__}: {exc}"
        )
    except Exception as exc:
        return TaskOutcome(
            task_id, "skipped", f"failed reading task.toml: {type(exc).__name__}: {exc}"
        )

    metadata = project_task_config(config_dict)

    # Descriptive columns on tasks -- mirrors tasks.py's
    # _apply_descriptive_metadata, but stamps BACKFILL instead of CLIENT.
    task.description = metadata.description
    task.category = metadata.category
    task.category_raw = metadata.category_raw
    task.author_name = metadata.author_name
    task.author_email = metadata.author_email
    task.author_organization = metadata.author_organization
    task.expert_time_hours = metadata.expert_time_hours
    task.metadata_source = MetadataSource.BACKFILL
    task.metadata_updated_at = utcnow()

    # Runtime + immutable snapshot columns on the current task_versions row
    # -- mirrors tasks.py's _apply_version_metadata.
    version.allow_internet = metadata.allow_internet
    version.cpus = metadata.cpus
    version.memory_mb = metadata.memory_mb
    version.storage_mb = metadata.storage_mb
    version.gpus = metadata.gpus
    version.gpu_types = metadata.gpu_types or None
    version.agent_timeout_sec = metadata.agent_timeout_sec
    version.verifier_timeout_sec = metadata.verifier_timeout_sec
    version.build_timeout_sec = metadata.build_timeout_sec
    version.description_snapshot = metadata.description
    version.category_snapshot = metadata.category
    version.topic_tags_snapshot = metadata.topic_tags or None
    version.expert_time_hours_snapshot = metadata.expert_time_hours

    # SAVEPOINT per derived.py's docstring: rebuild_derived_tags hits the
    # DB directly and a DBAPIError (e.g. a deploy racing a migration) would
    # otherwise poison the whole per-task transaction.
    try:
        async with session.begin_nested():
            await rebuild_derived_tags(
                session, task_id=task.id, org_id=task.org_id, metadata=metadata
            )
    except DERIVED_TAG_ERRORS as exc:
        logger.warning("rebuild_derived_tags failed for task %s: %s", task.id, exc)

    await _maybe_recover_ci_pr_number(session, task=task, version=version)

    return TaskOutcome(task_id, "backfilled", category=metadata.category)


async def run_backfill(
    *,
    task_names: list[str] | None,
    execute: bool,
    force: bool,
    storage: StorageClient | None = None,
) -> BackfillSummary:
    """Examine every selected task and, when eligible, backfill its metadata.

    Dry-run (``execute=False``, the default) NEVER commits: each task's work
    happens in its own session, which is always rolled back unless the task
    was actually backfilled AND ``execute`` is True. One session per task
    also means a DB error on one task can never poison another's.
    """
    storage = storage or get_storage_client()
    summary = BackfillSummary()

    async with async_session_maker() as select_session:
        task_ids = await _select_task_ids(select_session, task_names=task_names)

    for task_id in task_ids:
        async with async_session_maker() as session:
            try:
                outcome = await _backfill_one_task(
                    session, task_id, force=force, storage=storage
                )
            except Exception as exc:  # never let one bad task abort the run
                await session.rollback()
                logger.exception("backfill crashed for task %s", task_id)
                summary.record(
                    TaskOutcome(task_id, "failed", f"{type(exc).__name__}: {exc}")
                )
                continue

            if outcome.status == "backfilled" and execute:
                await session.commit()
            else:
                await session.rollback()
            summary.record(outcome)

    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task-names",
        default="",
        help="Comma-separated task names or ids (matches '{name}-{suffix}' too).",
    )
    parser.add_argument(
        "--all", action="store_true", help="Examine every task, not just --task-names."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite metadata even when metadata_source=CLIENT.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write changes. Without this flag the run is a dry-run (default).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    if not args.all and not args.task_names:
        print("Pass --task-names a,b,c or --all.")
        return

    names = [n.strip() for n in args.task_names.split(",") if n.strip()] or None
    summary = asyncio.run(
        run_backfill(task_names=names, execute=args.execute, force=args.force)
    )
    summary.print_report()
    if not args.execute:
        print("\nDRY RUN -- no write. Re-run with --execute to backfill.")


if __name__ == "__main__":
    main()

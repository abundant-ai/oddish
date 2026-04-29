"""Write-side S3 key/prefix construction.

These functions are the single source of truth for the S3 paths Oddish
*writes*. Reads must prefer DB-stored path columns (``task_s3_key``,
``trial_s3_key``, ``expanded_manifest_key``); only fall back to these
helpers for legacy rows whose columns were never populated.

Every output is prefixed with ``settings.s3_write_prefix`` (empty in
prod, ``previews/pr-<N>/`` in preview deployments) so preview envs
namespace newly-written keys without any call-site changes. Keep this
module pure path-string construction — no S3 calls, no normalization
beyond the literal layout below.
"""

from __future__ import annotations

from oddish.config import settings

TASK_ARCHIVE_OBJECT_NAME = ".oddish-task.tar.gz"
EXPANDED_MANIFEST_OBJECT_NAME = ".oddish-manifest.json"
TRIAL_IMPORT_ARCHIVE_OBJECT_NAME = ".oddish-trial-import.tar.gz"


def _wp() -> str:
    """Per-environment write prefix; ``""`` in prod."""
    return settings.s3_write_prefix


def task_prefix(task_id: str) -> str:
    return f"{_wp()}tasks/{task_id}/"


def task_version_prefix(task_id: str, version: int) -> str:
    return f"{_wp()}tasks/{task_id}/v{version}/"


def task_expanded_prefix(task_id: str, version: int) -> str:
    return f"{_wp()}tasks/{task_id}/v{version}-files/"


def task_archive_key(task_id: str) -> str:
    return f"{task_prefix(task_id)}{TASK_ARCHIVE_OBJECT_NAME}"


def task_archive_key_for_version(task_id: str, version: int) -> str:
    return f"{task_version_prefix(task_id, version)}{TASK_ARCHIVE_OBJECT_NAME}"


def task_archive_key_from_prefix(normalized_prefix: str) -> str:
    """Append the archive object name to an already-normalized task prefix.

    Callers pass in a prefix that already came from a write helper or a
    DB-stored path column, so it already carries the env prefix (or
    none). This helper does *not* prepend ``s3_write_prefix`` again — it
    just sticks the archive object name on the end.
    """
    return f"{normalized_prefix}{TASK_ARCHIVE_OBJECT_NAME}"


def trial_prefix(trial_id: str) -> str:
    """Return an S3 prefix for trial artifacts.

    Prefers nesting under the task prefix (``tasks/<task_id>/trials/<trial_id>/``)
    because some S3 backends/policies only allow writes under ``tasks/``.
    Trial IDs are generated as ``{task_id}-{index}`` where ``index`` is an
    integer, but ``task_id`` itself can contain dashes — split from the right.
    """
    task_id, sep, maybe_index = trial_id.rpartition("-")
    if sep and maybe_index.isdigit() and task_id:
        return f"{_wp()}tasks/{task_id}/trials/{trial_id}/"
    return f"{_wp()}trials/{trial_id}/"


def trial_import_archive_key(trial_id: str) -> str:
    return f"{trial_prefix(trial_id)}{TRIAL_IMPORT_ARCHIVE_OBJECT_NAME}"

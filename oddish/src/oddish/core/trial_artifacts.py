from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Protocol

from fastapi import HTTPException

from oddish.db.storage import (
    StorageClient,
    resolve_trial_s3_prefix,
    sanitize_s3_key_chars,
)


class TrialArtifactPointer(Protocol):
    id: str
    trial_s3_key: str | None


class TrialArtifactMode(str, Enum):
    """How a trial's readable artifact directory was identified."""

    EXACT = "exact"
    LEGACY = "legacy"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TrialArtifactLayout:
    """The one directory from which readers may load a trial's artifacts."""

    mode: TrialArtifactMode
    attempt_prefix: str
    artifact_prefix: str | None
    manifest: dict | None = None
    listed_keys: tuple[str, ...] | None = None


def normalize_trial_relative_path(file_path: str) -> str:
    """Normalize one trial-relative path and reject absolute or parent paths."""
    raw = file_path.replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid file path")
    parts = PurePosixPath(raw).parts
    if ".." in parts:
        raise HTTPException(status_code=400, detail="Invalid file path")
    normalized = str(PurePosixPath(*parts))
    if normalized in ("", ".", "/"):
        raise HTTPException(status_code=400, detail="Invalid file path")
    return normalized


def trial_name_from_manifest(manifest: object) -> str | None:
    """Return the sole Harbor child name, or None for a root-only failure."""
    if not isinstance(manifest, dict):
        raise ValueError("result.json must contain a JSON object")
    trial_results = manifest.get("trial_results")
    if not isinstance(trial_results, list):
        raise ValueError("result.json trial_results must be a list")
    if not trial_results:
        return None
    if len(trial_results) != 1 or not isinstance(trial_results[0], dict):
        raise ValueError("result.json must identify exactly one Harbor trial")
    trial_name = trial_results[0].get("trial_name")
    if not isinstance(trial_name, str) or not trial_name.strip():
        raise ValueError("result.json trial_name must be a non-empty string")
    trial_name = trial_name.strip()
    trial_name_path = PurePosixPath(trial_name)
    if (
        trial_name_path.is_absolute()
        or len(trial_name_path.parts) != 1
        or trial_name_path.parts[0] in (".", "..")
    ):
        raise ValueError("result.json trial_name must be one directory name")
    return trial_name


def _is_attempt_scoped_key(key: str, root_prefix: str) -> bool:
    """Whether an object belongs to the immutable per-attempt layout."""
    parts = PurePosixPath(key.removeprefix(root_prefix)).parts
    if not parts:
        return False
    if re.fullmatch(r"attempt-[1-9]\d*", parts[0]):
        return True
    return (
        len(parts) > 1
        and parts[0].startswith("analysis-")
        and re.fullmatch(r"attempt-[1-9]\d*", parts[1]) is not None
    )


async def resolve_trial_artifact_layout(
    trial: TrialArtifactPointer,
    storage: StorageClient,
) -> TrialArtifactLayout:
    """Resolve the exact Harbor child directory for the current attempt.

    Historical attempts without a root ``result.json`` remain eligible for
    deterministic fallback lookup. Once the manifest exists, malformed data or
    a missing selected artifact must not expose a sibling retry directory.
    """
    attempt_prefix = resolve_trial_s3_prefix(
        trial.id,
        trial_s3_key=trial.trial_s3_key,
    )
    listed_keys: tuple[str, ...] | None = None
    if trial.trial_s3_key is None:
        listed_keys = tuple(sorted(await storage.list_keys(attempt_prefix)))
        if any(_is_attempt_scoped_key(key, attempt_prefix) for key in listed_keys):
            return TrialArtifactLayout(
                TrialArtifactMode.UNAVAILABLE,
                attempt_prefix,
                None,
                listed_keys=listed_keys,
            )

    manifest_key = f"{attempt_prefix}result.json"
    if not await storage.object_exists(manifest_key):
        return TrialArtifactLayout(
            TrialArtifactMode.LEGACY,
            attempt_prefix,
            attempt_prefix,
            listed_keys=listed_keys,
        )

    try:
        manifest = json.loads(await storage.download_text(manifest_key))
        trial_name = trial_name_from_manifest(manifest)
    except (ValueError, TypeError, json.JSONDecodeError):
        return TrialArtifactLayout(
            TrialArtifactMode.UNAVAILABLE,
            attempt_prefix,
            None,
        )

    artifact_prefix = (
        attempt_prefix
        if trial_name is None
        else f"{attempt_prefix}{sanitize_s3_key_chars(trial_name)}/"
    )
    return TrialArtifactLayout(
        TrialArtifactMode.EXACT,
        attempt_prefix,
        artifact_prefix,
        manifest=manifest,
    )

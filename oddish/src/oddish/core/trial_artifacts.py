from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Never, Protocol

from fastapi import HTTPException

from oddish.core.harbor_artifacts import ODDISH_TRIAL_NAME_KEY, validate_trial_name
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
    failure_reason: str | None = None


class AnalysisArtifactLayoutError(RuntimeError):
    """A newly uploaded analysis attempt cannot satisfy its read contract."""


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
    if ODDISH_TRIAL_NAME_KEY in manifest:
        return validate_trial_name(manifest[ODDISH_TRIAL_NAME_KEY])
    trial_results = manifest.get("trial_results")
    if not isinstance(trial_results, list):
        raise ValueError("result.json trial_results must be a list")
    if not trial_results:
        return None
    if len(trial_results) != 1 or not isinstance(trial_results[0], dict):
        raise ValueError("result.json must identify exactly one Harbor trial")
    return validate_trial_name(trial_results[0].get("trial_name"))


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
                failure_reason=(
                    "trial_s3_key is missing while attempt-scoped artifacts exist"
                ),
            )

    manifest_key = f"{attempt_prefix}result.json"
    if not await storage.object_exists(manifest_key):
        return TrialArtifactLayout(
            TrialArtifactMode.LEGACY,
            attempt_prefix,
            attempt_prefix,
            listed_keys=listed_keys,
            failure_reason="result.json is missing",
        )

    try:
        manifest = json.loads(await storage.download_text(manifest_key))
    except (TypeError, json.JSONDecodeError):
        return TrialArtifactLayout(
            TrialArtifactMode.UNAVAILABLE,
            attempt_prefix,
            None,
            failure_reason="result.json is invalid JSON",
        )
    try:
        trial_name = trial_name_from_manifest(manifest)
    except ValueError as exc:
        return TrialArtifactLayout(
            TrialArtifactMode.UNAVAILABLE,
            attempt_prefix,
            None,
            failure_reason=str(exc),
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


async def validate_uploaded_analysis_artifacts(
    *,
    trial_id: str,
    trial_s3_key: str,
    required_artifact: str,
    has_trajectory: bool,
    storage: StorageClient,
) -> TrialArtifactLayout:
    """Prove a fresh analysis upload can be read before settlement succeeds."""

    async def reject(message: str) -> Never:
        try:
            uploaded_keys = sorted(await storage.list_keys(trial_s3_key))
            relative_files = [
                key.removeprefix(trial_s3_key) for key in uploaded_keys[:12]
            ]
            remaining = len(uploaded_keys) - len(relative_files)
            listing = f"uploaded_files={relative_files!r}"
            if remaining:
                listing += f" (+{remaining} more)"
        except Exception as exc:
            listing = f"uploaded_files_unavailable={type(exc).__name__}"
        raise AnalysisArtifactLayoutError(
            f"{message}; prefix={trial_s3_key!r}; {listing}"
        )

    @dataclass(frozen=True, slots=True)
    class UploadedAttempt:
        id: str
        trial_s3_key: str

    layout = await resolve_trial_artifact_layout(
        UploadedAttempt(id=trial_id, trial_s3_key=trial_s3_key), storage
    )
    if layout.mode is not TrialArtifactMode.EXACT or layout.manifest is None:
        await reject(layout.failure_reason or "result.json is unavailable")
    trial_name = trial_name_from_manifest(layout.manifest)
    if trial_name is None:
        await reject("result.json contains no trial_results")
    if layout.artifact_prefix == layout.attempt_prefix:
        await reject("result.json does not select a Harbor child directory")

    result_key = f"{layout.artifact_prefix}verifier/{required_artifact}"
    if not await storage.object_exists(result_key):
        await reject(f"selected Harbor child is missing verifier/{required_artifact}")
    if has_trajectory:
        trajectory_key = f"{layout.artifact_prefix}agent/trajectory.json"
        if not await storage.object_exists(trajectory_key):
            await reject(
                "outcome reports a trajectory but the selected Harbor child "
                "is missing agent/trajectory.json"
            )
    return layout

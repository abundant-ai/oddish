from __future__ import annotations

import asyncio
import json as _json
import logging
import mimetypes
import re
import time
from collections.abc import Hashable, MutableMapping
from contextlib import suppress
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from harbor.models.trial.paths import TrialPaths

from oddish.config import settings
from oddish.core.trial_artifacts import (
    TrialArtifactLayout,
    TrialArtifactMode,
    normalize_trial_relative_path,
    resolve_trial_artifact_layout,
    trial_name_from_manifest,
)
from oddish.db import TrialModel, get_storage_client
from oddish.db.storage import (
    StorageClient,
    _cleanup_temp_directory,
    resolve_trial_directory,
)
from oddish.workers.agents.grok_build_trajectory import (
    convert_grok_build_json_text_to_trajectory,
)

_CACHE_TTL_SECONDS = 120.0
_CACHE_MAX_ENTRIES = 128
_STRUCTURED_LOGS_CACHE: dict[tuple[str, int, str | None], tuple[float, dict]] = {}
_TRAJECTORY_CACHE: dict[tuple[str, int, str | None], tuple[float, dict | None]] = {}
_PROBE_ARTIFACTS_CACHE: dict[tuple[str, int, str | None], tuple[float, dict]] = {}
_STRUCTURED_LOGS_LOCKS: dict[tuple[str, int, str | None], asyncio.Lock] = {}
_TRAJECTORY_LOCKS: dict[tuple[str, int, str | None], asyncio.Lock] = {}
_PROBE_ARTIFACTS_LOCKS: dict[tuple[str, int, str | None], asyncio.Lock] = {}
_ARTIFACT_FALLBACK_ERRORS = (
    BotoCoreError,
    ClientError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)
logger = logging.getLogger(__name__)

_EMPTY_PROBE_ARTIFACTS: dict = {
    "trajectory": None,
    "verifier_stdout": None,
    "agent_messages": [],
    "watchdog_log": None,
}


def _cache_get[K: Hashable, T](
    cache: MutableMapping[K, tuple[float, T]], key: K
) -> T | None:
    entry = cache.get(key)
    if not entry:
        return None
    timestamp, value = entry
    if time.monotonic() - timestamp > _CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    return value


def _cache_set[K: Hashable, T](
    cache: MutableMapping[K, tuple[float, T]], key: K, value: T
) -> None:
    cache[key] = (time.monotonic(), value)
    if len(cache) <= _CACHE_MAX_ENTRIES:
        return
    oldest_key = min(cache.items(), key=lambda item: item[1][0])[0]
    cache.pop(oldest_key, None)


def _get_lock[K: Hashable](locks: dict[K, asyncio.Lock], key: K) -> asyncio.Lock:
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _should_cache_trial(trial: TrialModel) -> bool:
    return trial.finished_at is not None


def _resolve_local_job_dir(trial: TrialModel) -> Path | None:
    """Resolve and validate the local Harbor job directory for a trial.

    Returns ``None`` (not a 403) when ``harbor_result_path`` points outside the
    current container's ``harbor_jobs_dir``.  This happens for trials run
    before the Modal Volume was removed (they stored ``/data/harbor/...``
    paths that no longer resolve on the ephemeral-``/tmp`` containers);
    those trials just have no local fallback and should fall through to
    the "no trajectory / no result" branch rather than surfacing a
    spurious auth-looking error.  The path comes from our own DB, not
    user input, so there's no traversal risk to guard against here.
    """
    if not trial.harbor_result_path:
        return None

    result_path = Path(trial.harbor_result_path)
    base_dir = Path(settings.harbor_jobs_dir).resolve()
    try:
        result_path_resolved = result_path.resolve()
    except (OSError, RuntimeError):
        return None

    if (
        base_dir not in result_path_resolved.parents
        and result_path_resolved != base_dir
    ):
        return None

    job_dir = result_path_resolved.parent
    if not job_dir.exists() or not job_dir.is_dir():
        return None
    return job_dir


def _resolve_local_trial_paths(trial: TrialModel) -> TrialPaths | None:
    """Resolve Harbor trial directory for a trial's local artifacts.

    Harbor writes a job-level result at `<job_dir>/result.json` and per-trial
    artifacts under child trial directories. For older layouts we also accept
    direct `agent/` and `verifier/` under `<job_dir>`.
    """
    job_dir = _resolve_local_job_dir(trial)
    if job_dir is None:
        return None

    result_path = Path(trial.harbor_result_path)
    if result_path.exists() and result_path.is_file():
        try:
            manifest = _json.loads(result_path.read_text(errors="replace"))
            trial_name = trial_name_from_manifest(manifest)
        except (OSError, TypeError, ValueError):
            return None
        if trial_name is None:
            return TrialPaths(job_dir)
        trial_dir = job_dir / trial_name
        if trial_dir.exists() and trial_dir.is_dir():
            return TrialPaths(trial_dir)
        return None

    # Backward-compatible flat layout without a Harbor result manifest.
    if (job_dir / "agent").exists() or (job_dir / "verifier").exists():
        return TrialPaths(job_dir)

    # Setup failures can still leave behind root-level debug logs plus a
    # synthetic result.json. Treat the job directory itself as the trial dir so
    # the structured-log view can surface those artifacts.
    for candidate in job_dir.iterdir():
        if not candidate.is_file():
            continue
        if candidate.suffix in (".json", ".patch"):
            continue
        if candidate.suffix in (".log", ".txt"):
            return TrialPaths(job_dir)

    return None


def _legacy_trial_candidate_keys(
    trial: TrialModel,
    s3_prefix: str,
    relative_path: str,
) -> list[str]:
    """Return deterministic candidates for layouts without a root manifest."""
    candidates = [f"{s3_prefix}{relative_path}"]
    for trial_name in (trial.name, "trial-0"):
        if trial_name:
            candidates.append(f"{s3_prefix}{trial_name}/{relative_path}")
    return list(dict.fromkeys(candidates))


def _legacy_trajectory_candidate_keys(trial: TrialModel, s3_prefix: str) -> list[str]:
    return _legacy_trial_candidate_keys(trial, s3_prefix, "agent/trajectory.json")


def _legacy_grok_build_candidate_keys(trial: TrialModel, s3_prefix: str) -> list[str]:
    return _legacy_trial_candidate_keys(trial, s3_prefix, "agent/grok-build.json")


async def _download_first_text(
    storage: StorageClient,
    candidates: list[str],
) -> str | None:
    for key in candidates:
        with suppress(*_ARTIFACT_FALLBACK_ERRORS):
            return await storage.download_text(key)
    return None


def _convert_grok_build_text_to_trajectory(
    text: str,
    *,
    model_name: str | None,
) -> dict | None:
    trajectory = convert_grok_build_json_text_to_trajectory(
        text,
        agent_version="unknown",
        model_name=model_name,
    )
    if trajectory is None:
        return None
    return trajectory.to_json_dict()


async def read_trial_logs(trial: TrialModel) -> dict:
    """Read trial logs from S3 or local storage."""
    storage = get_storage_client()
    layout = await resolve_trial_artifact_layout(trial, storage)
    if layout.mode is TrialArtifactMode.UNAVAILABLE:
        return {"trial_id": trial.id, "logs": ""}
    try:
        assert layout.artifact_prefix is not None
        logs = await storage.download_trial_logs(layout.artifact_prefix)
        if logs or layout.mode is TrialArtifactMode.EXACT:
            return {
                "trial_id": trial.id,
                "logs": logs,
                "s3_key": layout.artifact_prefix,
            }
    except Exception:
        if layout.mode is TrialArtifactMode.EXACT:
            raise

    job_dir_resolved = _resolve_local_job_dir(trial)
    if job_dir_resolved is None:
        return {"trial_id": trial.id, "logs": ""}

    logs_parts: list[str] = []
    for p in sorted(job_dir_resolved.rglob("*")):
        if not p.is_file():
            continue
        is_log_file = p.suffix in (".log", ".txt")
        is_log_dir = any(part in p.parts for part in ("logs", "agent", "verifier"))
        if not is_log_file and not is_log_dir:
            continue
        if p.suffix in (".json", ".patch"):
            continue
        rel = p.relative_to(job_dir_resolved)
        try:
            content = p.read_text(errors="replace")
        except OSError as e:
            content = f"[failed to read {p.name}: {e}]"
        logs_parts.append(f"=== {rel} ===\n{content}\n")

    return {"trial_id": trial.id, "logs": "\n".join(logs_parts) if logs_parts else ""}


async def _read_trial_logs_structured_uncached(trial: TrialModel) -> dict:
    """Read trial logs structured by category (agent, verifier, exception).

    Uses parallel S3 downloads for improved performance.
    """
    result: dict = {
        "trial_id": trial.id,
        "agent": {"oracle": None, "setup": None, "commands": []},
        "verifier": {"stdout": None, "stderr": None},
        "other": [],  # Fallback for unrecognized log files
        "exception": trial.error_message,
    }

    storage = get_storage_client()
    layout = await resolve_trial_artifact_layout(trial, storage)
    if layout.mode is TrialArtifactMode.UNAVAILABLE:
        return result
    try:
        assert layout.artifact_prefix is not None
        s3_prefix = layout.artifact_prefix
        files = await storage.list_keys(s3_prefix)

        # Phase 1: Categorize files and plan downloads
        # Each entry: (key, category, extra_info)
        # category: "oracle", "setup", "command", "verifier_stdout", "verifier_stderr", "other"
        download_plan: list[tuple[str, str, str | None]] = []
        matched_keys: set[str] = set()

        # Track first matches for single-value fields
        oracle_key: str | None = None
        setup_key: str | None = None
        verifier_stdout_key: str | None = None
        verifier_stderr_key: str | None = None
        exception_key: str | None = None

        for key in files:
            # Agent logs
            if key.endswith(("/agent/oracle.txt", "/oracle.txt")):
                if oracle_key is None:
                    oracle_key = key
                    download_plan.append((key, "oracle", None))
                    matched_keys.add(key)
            elif key.endswith(("/agent/setup/stdout.txt", "/setup/stdout.txt")):
                if setup_key is None:
                    setup_key = key
                    download_plan.append((key, "setup", None))
                    matched_keys.add(key)
            elif "/agent/command-" in key and key.endswith("/stdout.txt"):
                match = re.search(r"(command-\d+)/stdout\.txt$", key)
                if match:
                    cmd_name = match.group(1)
                    download_plan.append((key, "command", cmd_name))
                    matched_keys.add(key)
            # Verifier logs
            elif key.endswith(("/verifier/test-stdout.txt", "/test-stdout.txt")):
                if verifier_stdout_key is None:
                    verifier_stdout_key = key
                    download_plan.append((key, "verifier_stdout", None))
                    matched_keys.add(key)
            elif key.endswith(("/verifier/test-stderr.txt", "/test-stderr.txt")):
                if verifier_stderr_key is None:
                    verifier_stderr_key = key
                    download_plan.append((key, "verifier_stderr", None))
                    matched_keys.add(key)
            elif key.endswith("/exception.txt") and exception_key is None:
                exception_key = key
                download_plan.append((key, "exception", None))
                matched_keys.add(key)

        # Add other log files that weren't matched
        for key in files:
            if key in matched_keys:
                continue
            s3_path = Path(key)
            is_log_file = s3_path.suffix in (".log", ".txt")
            is_log_dir = any(
                part in s3_path.parts for part in ("logs", "agent", "verifier")
            )
            if (is_log_file or is_log_dir) and s3_path.suffix not in (
                ".json",
                ".patch",
            ):
                rel_path = key.replace(s3_prefix, "").strip("/")
                download_plan.append((key, "other", rel_path))

        # Phase 2: Download all files in parallel
        if download_plan:

            async def safe_download(key: str) -> str | None:
                try:
                    return await storage.download_text(key)
                except Exception:
                    if layout.mode is TrialArtifactMode.EXACT:
                        raise
                    return None

            download_tasks = [safe_download(key) for key, _, _ in download_plan]
            contents = await asyncio.gather(*download_tasks)

            # Phase 3: Assign results to appropriate fields
            commands_list: list[tuple[str, str]] = []  # (cmd_name, content)
            other_list: list[tuple[str, str]] = []  # (rel_path, content)

            for (key, category, extra_info), content in zip(
                download_plan, contents, strict=False
            ):
                if content is None:
                    continue

                if category == "oracle":
                    result["agent"]["oracle"] = content
                elif category == "setup":
                    result["agent"]["setup"] = content
                elif category == "command" and extra_info:
                    commands_list.append((extra_info, content))
                elif category == "verifier_stdout":
                    result["verifier"]["stdout"] = content
                elif category == "verifier_stderr":
                    result["verifier"]["stderr"] = content
                elif category == "exception":
                    result["exception"] = content
                elif category == "other" and extra_info:
                    other_list.append((extra_info, content))

            # Sort commands by name (command-0, command-1, etc.)
            commands_list.sort(key=lambda x: x[0])
            result["agent"]["commands"] = [
                {"name": name, "content": content} for name, content in commands_list
            ]

            # Add other logs
            result["other"] = [
                {"name": name, "content": content} for name, content in other_list
            ]

        if files or layout.mode is TrialArtifactMode.EXACT:
            return result
    except Exception:
        if layout.mode is TrialArtifactMode.EXACT:
            raise

    # Local path fallback
    if not trial.harbor_result_path:
        return result

    trial_paths = _resolve_local_trial_paths(trial)
    if trial_paths is None:
        return result

    trial_dir = trial_paths.trial_dir
    agent_dir = trial_paths.agent_dir
    verifier_dir = trial_paths.verifier_dir

    # Agent: oracle.txt
    oracle_path = agent_dir / "oracle.txt"
    if oracle_path.exists():
        with suppress(OSError):
            result["agent"]["oracle"] = oracle_path.read_text(errors="replace")

    # Agent: setup/stdout.txt
    setup_path = agent_dir / "setup" / "stdout.txt"
    if setup_path.exists():
        with suppress(OSError):
            result["agent"]["setup"] = setup_path.read_text(errors="replace")

    # Agent: command-*/stdout.txt
    for cmd_dir in sorted(agent_dir.glob("command-*")):
        stdout_path = cmd_dir / "stdout.txt"
        if stdout_path.exists():
            with suppress(OSError):
                content = stdout_path.read_text(errors="replace")
                result["agent"]["commands"].append(
                    {"name": cmd_dir.name, "content": content}
                )

    # Verifier: test-stdout.txt, test-stderr.txt
    stdout_path = trial_paths.test_stdout_path
    if stdout_path.exists():
        with suppress(OSError):
            result["verifier"]["stdout"] = stdout_path.read_text(errors="replace")

    stderr_path = trial_paths.test_stderr_path
    if stderr_path.exists():
        with suppress(OSError):
            result["verifier"]["stderr"] = stderr_path.read_text(errors="replace")

    exception_path = trial_dir / "exception.txt"
    if exception_path.exists():
        with suppress(OSError):
            result["exception"] = exception_path.read_text(errors="replace")

    # Capture other log files as fallback
    matched_paths: set[Path] = set()
    if agent_dir.exists():
        if (agent_dir / "oracle.txt").exists():
            matched_paths.add(agent_dir / "oracle.txt")
        if (agent_dir / "setup" / "stdout.txt").exists():
            matched_paths.add(agent_dir / "setup" / "stdout.txt")
        for cmd_dir in agent_dir.glob("command-*"):
            if (cmd_dir / "stdout.txt").exists():
                matched_paths.add(cmd_dir / "stdout.txt")
    if verifier_dir.exists():
        if (verifier_dir / "test-stdout.txt").exists():
            matched_paths.add(verifier_dir / "test-stdout.txt")
        if (verifier_dir / "test-stderr.txt").exists():
            matched_paths.add(verifier_dir / "test-stderr.txt")

    for p in sorted(trial_dir.rglob("*")):
        if not p.is_file() or p in matched_paths:
            continue
        is_log_file = p.suffix in (".log", ".txt")
        is_log_dir = any(part in p.parts for part in ("logs", "agent", "verifier"))
        if (is_log_file or is_log_dir) and p.suffix not in (".json", ".patch"):
            with suppress(OSError, ValueError):
                rel = p.relative_to(trial_dir)
                content = p.read_text(errors="replace")
                result["other"].append({"name": str(rel), "content": content})

    return result


async def read_trial_logs_structured(trial: TrialModel) -> dict:
    cache_key = (trial.id, trial.attempts, trial.trial_s3_key)
    if _should_cache_trial(trial):
        cached = _cache_get(_STRUCTURED_LOGS_CACHE, cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

    lock = _get_lock(_STRUCTURED_LOGS_LOCKS, cache_key)
    async with lock:
        if _should_cache_trial(trial):
            cached = _cache_get(_STRUCTURED_LOGS_CACHE, cache_key)
            if cached is not None:
                return cached  # type: ignore[return-value]

        result = await _read_trial_logs_structured_uncached(trial)
        if _should_cache_trial(trial):
            _cache_set(_STRUCTURED_LOGS_CACHE, cache_key, result)
        return result


async def _read_trial_probe_artifacts_uncached(trial: TrialModel) -> dict:
    """Resolve the probe `_artifacts` blob (agent transcript, verifier stdout,
    trajectory, watchdog log) that the probe result page renders.

    The local runner inlines this into ``trial.result["_artifacts"]``; cloud
    trials leave ``trial.result`` empty and keep the artifacts only in object
    storage. This downloads the trial directory on demand and extracts the same
    shape, so the UI shows the agent output for cloud trials too.
    """
    if isinstance(trial.result, dict):
        inlined = trial.result.get("_artifacts")
        if inlined:
            return inlined

    # Imported here (not at module load) to avoid pulling the worker analysis
    # stack into every API import.
    from oddish.worker.probe_analysis import extract_probe_artifacts

    storage = get_storage_client()
    layout = await resolve_trial_artifact_layout(trial, storage)
    if layout.mode is TrialArtifactMode.UNAVAILABLE:
        return _EMPTY_PROBE_ARTIFACTS
    assert layout.artifact_prefix is not None
    try:
        trial_dir, temp_dir, _ = await resolve_trial_directory(
            trial_id=trial.id,
            trial_s3_key=layout.artifact_prefix,
            trial_result_path=(
                trial.harbor_result_path
                if layout.mode is TrialArtifactMode.LEGACY
                else None
            ),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning(
            "Could not resolve trial dir for probe artifacts %s: %s", trial.id, exc
        )
        return _EMPTY_PROBE_ARTIFACTS

    try:
        return extract_probe_artifacts(trial_dir)
    finally:
        if temp_dir is not None:
            _cleanup_temp_directory(temp_dir)


async def read_trial_probe_artifacts(trial: TrialModel) -> dict:
    cache_key = (trial.id, trial.attempts, trial.trial_s3_key)
    if _should_cache_trial(trial):
        cached = _cache_get(_PROBE_ARTIFACTS_CACHE, cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

    lock = _get_lock(_PROBE_ARTIFACTS_LOCKS, cache_key)
    async with lock:
        if _should_cache_trial(trial):
            cached = _cache_get(_PROBE_ARTIFACTS_CACHE, cache_key)
            if cached is not None:
                return cached  # type: ignore[return-value]

        result = await _read_trial_probe_artifacts_uncached(trial)
        if _should_cache_trial(trial):
            _cache_set(_PROBE_ARTIFACTS_CACHE, cache_key, result)
        return result


_STEP_TRAJECTORY_KEY_RE = re.compile(r"/steps/([^/]+)/agent/trajectory\.json$")


def _first_step_timestamp(trajectory: dict) -> str:
    for step in trajectory.get("steps") or []:
        ts = step.get("timestamp")
        if ts:
            return str(ts)
    return "~"  # sorts after any ISO timestamp; keeps listing order for ties


def _sum_numeric_metrics(dicts: list[dict]) -> dict:
    """Sum numeric leaves across final_metrics dicts; first value wins otherwise."""
    merged: dict = {}
    for d in dicts:
        for key, value in d.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                prev = merged.get(key)
                if not isinstance(prev, (int, float)) or isinstance(prev, bool):
                    # A step may carry null/str where another has a number;
                    # never let that TypeError drop the whole trajectory.
                    prev = 0
                merged[key] = prev + value
            elif isinstance(value, dict):
                merged[key] = _sum_numeric_metrics(
                    [v for v in (merged.get(key), value) if isinstance(v, dict)]
                )
            elif key not in merged:
                merged[key] = value
    return merged


def _merge_step_trajectories(named: list[tuple[str, dict]]) -> dict:
    """Merge per-step ATIF trajectories of a multi-step trial into one document.

    Harbor step tasks store one trajectory per step under
    ``steps/<name>/agent/trajectory.json``. Steps are ordered by their first
    inner-step timestamp (harbor runs them sequentially), inner step_ids are
    re-sequenced, and each inner step is tagged with its harbor step name in
    ``extra.harbor_step`` so viewers can render the boundary.
    """
    ordered = sorted(named, key=lambda nt: (_first_step_timestamp(nt[1]), nt[0]))
    base = dict(ordered[0][1])
    merged_steps: list[dict] = []
    subagents: list = []
    for step_name, trajectory in ordered:
        for inner in trajectory.get("steps") or []:
            inner = dict(inner)
            extra = dict(inner.get("extra") or {})
            extra.setdefault("harbor_step", step_name)
            inner["extra"] = extra
            inner["step_id"] = len(merged_steps) + 1
            merged_steps.append(inner)
        subagents.extend(trajectory.get("subagent_trajectories") or [])
    base["steps"] = merged_steps
    metrics = [
        t.get("final_metrics")
        for _, t in ordered
        if isinstance(t.get("final_metrics"), dict)
    ]
    if metrics:
        base["final_metrics"] = _sum_numeric_metrics(metrics)
    if subagents:
        base["subagent_trajectories"] = subagents
    return base


async def _read_step_trajectories(
    storage: StorageClient,
    files: list[str],
) -> dict | None:
    """Read and merge ``steps/*/agent/trajectory.json`` from listed keys."""
    try:
        named: list[tuple[str, dict]] = []
        for key in files:
            match = _STEP_TRAJECTORY_KEY_RE.search(key)
            if not match:
                continue
            content = await storage.download_text(key)
            if content:
                named.append((match.group(1), _json.loads(content)))
        if named:
            return _merge_step_trajectories(named)
    except _ARTIFACT_FALLBACK_ERRORS:
        return None
    except (_json.JSONDecodeError, TypeError, ValueError):
        return None
    return None


async def _read_step_trajectories_exact(
    storage: StorageClient,
    artifact_prefix: str,
) -> dict | None:
    """Merge step trajectories in EXACT mode without scanning any prefix.

    Step names come from the trial's own ``result.json`` (``step_results``),
    so every S3 access is an exact key lookup — EXACT reads must never list
    sibling directories.
    """
    result_key = f"{artifact_prefix}result.json"
    try:
        if not await storage.object_exists(result_key):
            return None
        result = _json.loads(await storage.download_text(result_key) or "{}")
        step_names = [
            s.get("step_name")
            for s in result.get("step_results") or []
            if isinstance(s, dict) and s.get("step_name")
        ]
        if not step_names:
            return None
        keys = []
        for name in step_names:
            key = f"{artifact_prefix}steps/{name}/agent/trajectory.json"
            if await storage.object_exists(key):
                keys.append(key)
        return await _read_step_trajectories(storage, keys)
    except _ARTIFACT_FALLBACK_ERRORS:
        return None
    except (_json.JSONDecodeError, TypeError, ValueError):
        return None


async def _read_trial_trajectory_from_s3(
    trial: TrialModel,
    storage: StorageClient,
    layout: TrialArtifactLayout,
) -> dict | None:
    if layout.mode is TrialArtifactMode.EXACT:
        assert layout.artifact_prefix is not None
        trajectory_key = f"{layout.artifact_prefix}agent/trajectory.json"
        if await storage.object_exists(trajectory_key):
            try:
                content = await storage.download_text(trajectory_key)
                if content:
                    parsed: dict = _json.loads(content)
                    return parsed
            except (_json.JSONDecodeError, TypeError, ValueError):
                return None

        # Multi-step tasks store one trajectory per step instead of a
        # root agent/trajectory.json (harbor [[steps]], 2026-04-22).
        merged = await _read_step_trajectories_exact(storage, layout.artifact_prefix)
        if merged is not None:
            return merged

        grok_key = f"{layout.artifact_prefix}agent/grok-build.json"
        if not await storage.object_exists(grok_key):
            return None
        try:
            content = await storage.download_text(grok_key)
            if not content:
                return None
            return _convert_grok_build_text_to_trajectory(
                content,
                model_name=trial.model,
            )
        except (_json.JSONDecodeError, TypeError, ValueError):
            return None

    if layout.mode is TrialArtifactMode.UNAVAILABLE:
        return None

    # Imported and historical trials may predate the root-manifest contract.
    for trajectory_key in _legacy_trajectory_candidate_keys(
        trial, layout.attempt_prefix
    ):
        with suppress(*_ARTIFACT_FALLBACK_ERRORS):
            content = await storage.download_text(trajectory_key)
            if content:
                parsed = _json.loads(content)
                return parsed

    for grok_key in _legacy_grok_build_candidate_keys(trial, layout.attempt_prefix):
        with suppress(*_ARTIFACT_FALLBACK_ERRORS):
            content = await storage.download_text(grok_key)
            if content:
                parsed = _convert_grok_build_text_to_trajectory(
                    content,
                    model_name=trial.model,
                )
                if parsed:
                    return parsed

    try:
        files = (
            list(layout.listed_keys)
            if layout.listed_keys is not None
            else sorted(await storage.list_keys(layout.attempt_prefix))
        )
        step_keys = [f for f in files if _STEP_TRAJECTORY_KEY_RE.search(f)]
        if step_keys:
            merged = await _read_step_trajectories(storage, step_keys)
            if merged is not None:
                return merged
        grok_build_keys: list[str] = []
        for f in files:
            if f.endswith(
                "/agent/trajectory.json"
            ) and not _STEP_TRAJECTORY_KEY_RE.search(f):
                content = await storage.download_text(f)
                if content:
                    parsed = _json.loads(content)
                    return parsed
            if f.endswith("/agent/grok-build.json"):
                grok_build_keys.append(f)
        for f in grok_build_keys:
            content = await storage.download_text(f)
            if content:
                parsed = _convert_grok_build_text_to_trajectory(
                    content,
                    model_name=trial.model,
                )
                if parsed:
                    return parsed
    except _ARTIFACT_FALLBACK_ERRORS as e:
        logger.debug(
            "No trajectory in S3 for %s at %s: %s",
            trial.id,
            layout.attempt_prefix,
            e,
        )
    return None


def _read_local_trial_trajectory(trial: TrialModel) -> dict | None:
    """Read a trajectory from the local Harbor directory for legacy runs."""

    if not trial.harbor_result_path:
        return None

    trial_paths = _resolve_local_trial_paths(trial)
    if trial_paths is None:
        return None
    trajectory_path = trial_paths.agent_dir / "trajectory.json"

    try:
        trajectory_path_resolved = trajectory_path.resolve()
    except (OSError, RuntimeError):
        return None

    if not trajectory_path_resolved.exists() or not trajectory_path_resolved.is_file():
        grok_build_path = trial_paths.agent_dir / "grok-build.json"
        try:
            grok_build_path_resolved = grok_build_path.resolve()
        except (OSError, RuntimeError):
            return None
        if (
            not grok_build_path_resolved.exists()
            or not grok_build_path_resolved.is_file()
        ):
            return None
        try:
            return _convert_grok_build_text_to_trajectory(
                grok_build_path_resolved.read_text(errors="replace"),
                model_name=trial.model,
            )
        except (OSError, TypeError, ValueError):
            return None

    try:
        local_parsed: dict = _json.loads(
            trajectory_path_resolved.read_text(errors="replace")
        )
        return local_parsed
    except (OSError, TypeError, ValueError):
        return None


async def _read_trial_trajectory_uncached(trial: TrialModel) -> dict | None:
    """Read ATIF trajectory.json for a trial."""
    storage = get_storage_client()
    layout = await resolve_trial_artifact_layout(trial, storage)
    trajectory = await _read_trial_trajectory_from_s3(trial, storage, layout)
    if trajectory is not None or layout.mode is not TrialArtifactMode.LEGACY:
        return trajectory
    return _read_local_trial_trajectory(trial)


async def read_trial_trajectory(trial: TrialModel) -> dict | None:
    cache_key = (trial.id, trial.attempts, trial.trial_s3_key)
    if _should_cache_trial(trial):
        cached = _cache_get(_TRAJECTORY_CACHE, cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

    lock = _get_lock(_TRAJECTORY_LOCKS, cache_key)
    async with lock:
        if _should_cache_trial(trial):
            cached = _cache_get(_TRAJECTORY_CACHE, cache_key)
            if cached is not None:
                return cached  # type: ignore[return-value]

        result = await _read_trial_trajectory_uncached(trial)
        if _should_cache_trial(trial):
            _cache_set(_TRAJECTORY_CACHE, cache_key, result)
        return result


async def read_trial_summary_inputs(
    trial: TrialModel,
) -> tuple[dict | None, str | None, str | None]:
    """Read the trajectory, instruction, and verifier output from one layout."""
    storage = get_storage_client()
    layout = await resolve_trial_artifact_layout(trial, storage)
    trajectory = await _read_trial_trajectory_from_s3(trial, storage, layout)
    if trajectory is None and layout.mode is TrialArtifactMode.LEGACY:
        trajectory = _read_local_trial_trajectory(trial)

    if layout.mode is TrialArtifactMode.EXACT:
        assert layout.artifact_prefix is not None
        instruction_keys = [f"{layout.artifact_prefix}task/instruction.md"]
        verifier_keys = [
            f"{layout.artifact_prefix}verifier/test-stdout.txt",
            f"{layout.artifact_prefix}verifier/stdout.txt",
        ]
    elif layout.mode is TrialArtifactMode.UNAVAILABLE:
        return trajectory, None, None
    else:
        instruction_keys = _legacy_trial_candidate_keys(
            trial,
            layout.attempt_prefix,
            "task/instruction.md",
        )
        verifier_keys = []
        for relative_path in (
            "verifier/test-stdout.txt",
            "verifier/stdout.txt",
        ):
            verifier_keys.extend(
                _legacy_trial_candidate_keys(
                    trial,
                    layout.attempt_prefix,
                    relative_path,
                )
            )
        verifier_keys = list(dict.fromkeys(verifier_keys))

    if layout.mode is TrialArtifactMode.EXACT:

        async def download_exact(candidates: list[str]) -> str | None:
            for key in candidates:
                if await storage.object_exists(key):
                    return await storage.download_text(key)
            return None

        instruction, verifier_output = await asyncio.gather(
            download_exact(instruction_keys),
            download_exact(verifier_keys),
        )
    else:
        instruction, verifier_output = await asyncio.gather(
            _download_first_text(storage, instruction_keys),
            _download_first_text(storage, verifier_keys),
        )
    return trajectory, instruction, verifier_output


async def read_trial_agent_file(
    trial: TrialModel,
    file_path: str,
) -> tuple[bytes, str]:
    """Read a file from the trial's `agent/` directory."""
    normalized_path = normalize_trial_relative_path(file_path)
    media_type, _ = mimetypes.guess_type(normalized_path)
    if media_type is None:
        media_type = "application/octet-stream"

    storage = get_storage_client()
    layout = await resolve_trial_artifact_layout(trial, storage)

    if layout.mode is TrialArtifactMode.EXACT:
        assert layout.artifact_prefix is not None
        key = f"{layout.artifact_prefix}agent/{normalized_path}"
        if not await storage.object_exists(key):
            raise HTTPException(status_code=404, detail="File not found")
        return await storage.download_bytes(key), media_type

    if layout.mode is TrialArtifactMode.LEGACY:
        direct_key = f"{layout.attempt_prefix}agent/{normalized_path}"
        try:
            content = await storage.download_bytes(direct_key)
            return content, media_type
        except _ARTIFACT_FALLBACK_ERRORS:
            logger.debug(
                "No direct legacy agent file for %s at %s",
                trial.id,
                direct_key,
                exc_info=True,
            )

        try:
            suffix = f"/agent/{normalized_path}"
            files = (
                layout.listed_keys
                if layout.listed_keys is not None
                else await storage.list_keys(layout.attempt_prefix)
            )
            for key in files:
                if key.endswith(suffix):
                    content = await storage.download_bytes(key)
                    return content, media_type
        except _ARTIFACT_FALLBACK_ERRORS as e:
            logger.debug(
                "No agent file in S3 for %s at %s: %s",
                trial.id,
                layout.attempt_prefix,
                e,
            )

    if layout.mode is TrialArtifactMode.UNAVAILABLE:
        raise HTTPException(status_code=404, detail="File not found")

    if not trial.harbor_result_path:
        raise HTTPException(status_code=404, detail="Trial has no local result path")

    trial_paths = _resolve_local_trial_paths(trial)
    if trial_paths is None:
        raise HTTPException(status_code=404, detail="Trial has no local result path")

    try:
        file_path_resolved = (trial_paths.agent_dir / normalized_path).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="File not found")

    if trial_paths.trial_dir.resolve() not in file_path_resolved.parents:
        raise HTTPException(
            status_code=403,
            detail="Refusing to read file outside harbor_jobs_dir",
        )

    if not file_path_resolved.exists() or not file_path_resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        return file_path_resolved.read_bytes(), media_type
    except OSError:
        raise HTTPException(status_code=404, detail="File not found")


async def read_trial_result(trial: TrialModel) -> dict:
    """Read result.json for a trial."""
    storage = get_storage_client()
    layout = await resolve_trial_artifact_layout(trial, storage)
    if layout.mode is TrialArtifactMode.EXACT:
        assert layout.manifest is not None
        return layout.manifest
    if layout.mode is TrialArtifactMode.UNAVAILABLE:
        raise HTTPException(
            status_code=404,
            detail=f"No authoritative result found for {trial.id}",
        )
    result_json = await storage.get_trial_result_json(layout.attempt_prefix)
    if result_json:
        return result_json

    # Local path: read result.json from harbor_result_path
    if not trial.harbor_result_path:
        raise HTTPException(
            status_code=404, detail=f"Trial {trial.id} has no local result path"
        )

    if _resolve_local_job_dir(trial) is None:
        raise HTTPException(
            status_code=404, detail=f"Local result not found for {trial.id}"
        )
    result_path_resolved = Path(trial.harbor_result_path).resolve()

    if not result_path_resolved.exists() or not result_path_resolved.is_file():
        raise HTTPException(
            status_code=404, detail=f"Local result not found for {trial.id}"
        )

    try:
        parsed: dict = _json.loads(result_path_resolved.read_text(errors="replace"))
        return parsed
    except (OSError, TypeError, ValueError) as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to parse local result.json: {e}"
        )


async def debug_trial_files(trial: TrialModel) -> dict:
    """Debug endpoint logic: list all files in S3 for a trial."""
    result = {
        "trial_id": trial.id,
        "trial_s3_key": trial.trial_s3_key,
        "computed_prefix": StorageClient._trial_prefix(trial.id),
        "harbor_result_path": trial.harbor_result_path,
        "files": [],
        "trajectory_files": [],
        "error": None,
    }

    s3_prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial.id)
    result["using_prefix"] = s3_prefix

    storage = get_storage_client()
    try:
        # List all files under this prefix
        files = await storage.list_keys(s3_prefix)
        result["files"] = files
        # Find any trajectory files
        result["trajectory_files"] = [f for f in files if "trajectory.json" in f]
    except _ARTIFACT_FALLBACK_ERRORS as e:
        result["error"] = f"Failed to list files: {e!s}"

    return result

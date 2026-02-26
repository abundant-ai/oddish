from __future__ import annotations

import asyncio
import json as _json
import logging
import mimetypes
import re
import time
from pathlib import Path, PurePosixPath
from typing import MutableMapping, TypeVar

from fastapi import HTTPException
from harbor.models.trial.paths import TrialPaths

from oddish.config import settings
from oddish.db import TrialModel, get_storage_client
from oddish.db.storage import StorageClient


_CACHE_TTL_SECONDS = 120.0
_CACHE_MAX_ENTRIES = 128
_STRUCTURED_LOGS_CACHE: dict[str, tuple[float, dict]] = {}
_TRAJECTORY_CACHE: dict[str, tuple[float, dict | None]] = {}
_STRUCTURED_LOGS_LOCKS: dict[str, asyncio.Lock] = {}
_TRAJECTORY_LOCKS: dict[str, asyncio.Lock] = {}
_T = TypeVar("_T")


def _cache_get(cache: MutableMapping[str, tuple[float, _T]], key: str) -> _T | None:
    entry = cache.get(key)
    if not entry:
        return None
    timestamp, value = entry
    if time.monotonic() - timestamp > _CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    return value


def _cache_set(
    cache: MutableMapping[str, tuple[float, _T]], key: str, value: _T
) -> None:
    cache[key] = (time.monotonic(), value)
    if len(cache) <= _CACHE_MAX_ENTRIES:
        return
    oldest_key = min(cache.items(), key=lambda item: item[1][0])[0]
    cache.pop(oldest_key, None)


def _get_lock(locks: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _should_cache_trial(trial: TrialModel) -> bool:
    return trial.finished_at is not None


def _resolve_local_trial_paths(trial: TrialModel) -> TrialPaths | None:
    """Resolve Harbor trial directory for a trial's local artifacts.

    Harbor writes a job-level result at `<job_dir>/result.json` and per-trial
    artifacts at `<job_dir>/<trial_name>/...`. For older layouts we also accept
    direct `agent/` and `verifier/` under `<job_dir>`.
    """
    if not trial.harbor_result_path:
        return None

    result_path = Path(trial.harbor_result_path)
    base_dir = Path(settings.harbor_jobs_dir).resolve()
    try:
        result_path_resolved = result_path.resolve()
    except Exception:
        return None

    if (
        base_dir not in result_path_resolved.parents
        and result_path_resolved != base_dir
    ):
        raise HTTPException(
            status_code=403, detail="Refusing to read trial outside harbor_jobs_dir"
        )

    job_dir = result_path_resolved.parent
    if not job_dir.exists() or not job_dir.is_dir():
        return None

    # Backward-compatible flat layout: logs directly under the job directory.
    if (job_dir / "agent").exists() or (job_dir / "verifier").exists():
        return TrialPaths(job_dir)

    # Harbor layout: one child directory per trial, each with result.json.
    child_results = sorted(
        job_dir.glob("*/result.json"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    if child_results:
        return TrialPaths(child_results[0].parent)

    return None


def _trajectory_candidate_keys(trial: TrialModel, s3_prefix: str) -> list[str]:
    """Return likely S3 keys for trajectory without listing whole prefixes."""
    candidates: list[str] = [f"{s3_prefix}agent/trajectory.json"]
    if trial.name:
        candidates.append(f"{s3_prefix}{trial.name}/agent/trajectory.json")
    # Common Harbor fallback naming convention.
    candidates.append(f"{s3_prefix}trial-0/agent/trajectory.json")
    return list(dict.fromkeys(candidates))


async def read_trial_logs(trial: TrialModel) -> dict:
    """Read trial logs from S3 or local storage."""
    # Prefer S3 if configured and we have a prefix
    if settings.s3_enabled:
        s3_prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial.id)
        storage = get_storage_client()
        try:
            logs = await storage.download_trial_logs(s3_prefix)
            return {"trial_id": trial.id, "logs": logs, "s3_key": s3_prefix}
        except Exception:
            # Fall back to local volume if S3 read fails
            pass

    # Local path (best-effort): infer job directory from harbor_result_path
    if not trial.harbor_result_path:
        return {"trial_id": trial.id, "logs": ""}

    result_path = Path(trial.harbor_result_path)
    job_dir = result_path.parent
    base_dir = Path(settings.harbor_jobs_dir).resolve()

    try:
        job_dir_resolved = job_dir.resolve()
    except Exception:
        return {"trial_id": trial.id, "logs": ""}

    # Safety: only allow reads under harbor_jobs_dir
    if base_dir not in job_dir_resolved.parents and job_dir_resolved != base_dir:
        raise HTTPException(
            status_code=403, detail="Refusing to read logs outside harbor_jobs_dir"
        )

    if not job_dir_resolved.exists() or not job_dir_resolved.is_dir():
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
        rel: Path | str
        try:
            rel = p.relative_to(job_dir_resolved)
        except Exception:
            rel = p.name
        try:
            content = p.read_text(errors="replace")
        except Exception as e:
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

    # Try S3 first
    if settings.s3_enabled:
        s3_prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial.id)
        storage = get_storage_client()
        try:
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

            for key in files:
                # Agent logs
                if key.endswith("/agent/oracle.txt") or key.endswith("/oracle.txt"):
                    if oracle_key is None:
                        oracle_key = key
                        download_plan.append((key, "oracle", None))
                        matched_keys.add(key)
                elif key.endswith("/agent/setup/stdout.txt") or key.endswith(
                    "/setup/stdout.txt"
                ):
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
                elif key.endswith("/verifier/test-stdout.txt") or key.endswith(
                    "/test-stdout.txt"
                ):
                    if verifier_stdout_key is None:
                        verifier_stdout_key = key
                        download_plan.append((key, "verifier_stdout", None))
                        matched_keys.add(key)
                elif key.endswith("/verifier/test-stderr.txt") or key.endswith(
                    "/test-stderr.txt"
                ):
                    if verifier_stderr_key is None:
                        verifier_stderr_key = key
                        download_plan.append((key, "verifier_stderr", None))
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
                    elif category == "other" and extra_info:
                        other_list.append((extra_info, content))

                # Sort commands by name (command-0, command-1, etc.)
                commands_list.sort(key=lambda x: x[0])
                result["agent"]["commands"] = [
                    {"name": name, "content": content}
                    for name, content in commands_list
                ]

                # Add other logs
                result["other"] = [
                    {"name": name, "content": content} for name, content in other_list
                ]

            return result
        except Exception:
            pass  # Fall through to local

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
        try:
            result["agent"]["oracle"] = oracle_path.read_text(errors="replace")
        except Exception:
            pass

    # Agent: setup/stdout.txt
    setup_path = agent_dir / "setup" / "stdout.txt"
    if setup_path.exists():
        try:
            result["agent"]["setup"] = setup_path.read_text(errors="replace")
        except Exception:
            pass

    # Agent: command-*/stdout.txt
    for cmd_dir in sorted(agent_dir.glob("command-*")):
        stdout_path = cmd_dir / "stdout.txt"
        if stdout_path.exists():
            try:
                content = stdout_path.read_text(errors="replace")
                result["agent"]["commands"].append(
                    {"name": cmd_dir.name, "content": content}
                )
            except Exception:
                pass

    # Verifier: test-stdout.txt, test-stderr.txt
    stdout_path = trial_paths.test_stdout_path
    if stdout_path.exists():
        try:
            result["verifier"]["stdout"] = stdout_path.read_text(errors="replace")
        except Exception:
            pass

    stderr_path = trial_paths.test_stderr_path
    if stderr_path.exists():
        try:
            result["verifier"]["stderr"] = stderr_path.read_text(errors="replace")
        except Exception:
            pass

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
            try:
                rel = p.relative_to(trial_dir)
                content = p.read_text(errors="replace")
                result["other"].append({"name": str(rel), "content": content})
            except Exception:
                pass

    return result


async def read_trial_logs_structured(trial: TrialModel) -> dict:
    cache_key = trial.id
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


async def _read_trial_trajectory_uncached(trial: TrialModel) -> dict | None:
    """Read ATIF trajectory.json for a trial."""
    # Try S3 first
    if settings.s3_enabled:
        s3_prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial.id)
        storage = get_storage_client()

        # Prefer direct key lookups to avoid expensive prefix listings.
        for trajectory_key in _trajectory_candidate_keys(trial, s3_prefix):
            try:
                content = await storage.download_text(trajectory_key)
                if content:
                    return _json.loads(content)
            except Exception:
                continue

        # If not found, look for trial subdirectory (Harbor's actual structure)
        try:
            # List files to find the trial subdirectory
            files = await storage.list_keys(s3_prefix)
            # Look for pattern like: s3_prefix/trial-0/agent/trajectory.json
            # or s3_prefix/some-name/agent/trajectory.json
            for f in files:
                if f.endswith("/agent/trajectory.json"):
                    content = await storage.download_text(f)
                    if content:
                        return _json.loads(content)
        except Exception as e:
            logging.getLogger(__name__).debug(
                f"No trajectory in S3 for {trial.id} at {s3_prefix}: {e}"
            )

    # Local path fallback
    if not trial.harbor_result_path:
        return None

    trial_paths = _resolve_local_trial_paths(trial)
    if trial_paths is None:
        return None
    trajectory_path = trial_paths.agent_dir / "trajectory.json"

    try:
        trajectory_path_resolved = trajectory_path.resolve()
    except Exception:
        return None

    if not trajectory_path_resolved.exists() or not trajectory_path_resolved.is_file():
        return None

    try:
        return _json.loads(trajectory_path_resolved.read_text(errors="replace"))
    except Exception:
        return None


async def read_trial_trajectory(trial: TrialModel) -> dict | None:
    cache_key = trial.id
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


def _normalize_relative_agent_path(file_path: str) -> str:
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


async def read_trial_agent_file(
    trial: TrialModel,
    file_path: str,
) -> tuple[bytes, str]:
    """Read a file from the trial's `agent/` directory."""
    normalized_path = _normalize_relative_agent_path(file_path)
    media_type, _ = mimetypes.guess_type(normalized_path)
    if media_type is None:
        media_type = "application/octet-stream"

    if settings.s3_enabled:
        s3_prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial.id)
        storage = get_storage_client()

        direct_key = f"{s3_prefix}agent/{normalized_path}"
        try:
            content = await storage.download_bytes(direct_key)
            return content, media_type
        except Exception:
            pass

        try:
            suffix = f"/agent/{normalized_path}"
            for key in await storage.list_keys(s3_prefix):
                if key.endswith(suffix):
                    content = await storage.download_bytes(key)
                    return content, media_type
        except Exception as e:
            logging.getLogger(__name__).debug(
                f"No agent file in S3 for {trial.id} at {s3_prefix}: {e}"
            )

    if not trial.harbor_result_path:
        raise HTTPException(status_code=404, detail="Trial has no local result path")

    trial_paths = _resolve_local_trial_paths(trial)
    if trial_paths is None:
        raise HTTPException(status_code=404, detail="Trial has no local result path")

    try:
        file_path_resolved = (trial_paths.agent_dir / normalized_path).resolve()
    except Exception:
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
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")


async def read_trial_result(trial: TrialModel) -> dict:
    """Read result.json for a trial."""
    # Prefer S3 if configured and we have a prefix
    if settings.s3_enabled:
        s3_prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial.id)
        storage = get_storage_client()
        try:
            result_json = await storage.get_trial_result_json(s3_prefix)
            if result_json:
                return result_json
        except Exception:
            # Fall back to local volume if S3 read fails
            pass

    # Local path: read result.json from harbor_result_path
    if not trial.harbor_result_path:
        raise HTTPException(
            status_code=404, detail=f"Trial {trial.id} has no local result path"
        )

    result_path = Path(trial.harbor_result_path)
    base_dir = Path(settings.harbor_jobs_dir).resolve()
    try:
        result_path_resolved = result_path.resolve()
    except Exception:
        raise HTTPException(
            status_code=404, detail=f"Local result not found for {trial.id}"
        )

    if (
        base_dir not in result_path_resolved.parents
        and result_path_resolved != base_dir
    ):
        raise HTTPException(
            status_code=403,
            detail="Refusing to read result outside harbor_jobs_dir",
        )

    if not result_path_resolved.exists() or not result_path_resolved.is_file():
        raise HTTPException(
            status_code=404, detail=f"Local result not found for {trial.id}"
        )

    try:
        return _json.loads(result_path_resolved.read_text(errors="replace"))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to parse local result.json: {e}"
        )

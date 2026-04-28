from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from collections.abc import Iterable
from typing import Any, cast

import httpx
import typer
import yaml
from rich.console import Console
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

from harbor.models.environment_type import EnvironmentType
from harbor.models.task.paths import TaskPaths
from harbor.models.task.task import Task
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.result import TrialResult
from harbor.viewer.scanner import JobScanner

from oddish.cli.config import get_auth_headers, error_console
from oddish.task_timeouts import (
    TaskTimeoutValidationError,
    validate_task_timeout_config,
)

console = Console()
TASK_SWEEP_TIMEOUT_SECONDS = 600.0


def format_reward_value(reward: float | None) -> str:
    if reward is None:
        return "-"
    if reward == 1:
        return "[green]✓[/green]"
    if reward == 0:
        return "[red]✗[/red]"
    return f"[yellow]{reward:.2f}[/yellow]"


# =============================================================================
# Task Path Resolution
# =============================================================================


def resolve_task_path(path_arg: Path | None, path_option: Path | None) -> Path | None:
    """Resolve task path from positional or --path option. Returns None if not provided."""
    if path_arg and path_option:
        error_console.print(
            "[red]Provide either a positional PATH or --path, not both.[/red]"
        )
        raise typer.Exit(1)
    task_path = path_option or path_arg
    if task_path and (not task_path.exists() or not task_path.is_dir()):
        error_console.print(f"[red]Invalid directory:[/red] {task_path}")
        raise typer.Exit(1)
    return task_path


def is_task_dir(path: Path) -> bool:
    """Check if a path is a valid Harbor task directory."""
    return cast(bool, TaskPaths(path).is_valid(disable_verification=False))


def validate_tasks(task_paths: list[Path]) -> list[Path]:
    """Validate task configs by loading each task with Harbor's Task model.

    Returns the list of valid task paths. Prints warnings for invalid tasks
    and exits if all tasks are invalid.
    """
    valid: list[Path] = []
    errors: list[tuple[Path, str]] = []

    for task_path in task_paths:
        try:
            Task(task_path)
            valid.append(task_path)
        except FileNotFoundError as e:
            errors.append((task_path, f"Missing file: {e.filename or e}"))
        except Exception as e:
            label = type(e).__name__
            errors.append((task_path, f"{label}: {e}"))

    if errors:
        error_console.print(
            f"\n[yellow]Task validation: {len(errors)} of {len(task_paths)} "
            f"task(s) have issues:[/yellow]"
        )
        for task_path, msg in errors:
            error_console.print(f"  [red]✗[/red] {task_path.name}: {msg}")

    if not valid:
        error_console.print("\n[red]All tasks failed validation. Nothing to run.[/red]")
        raise typer.Exit(1)

    if errors:
        error_console.print(
            f"\n[dim]Continuing with {len(valid)} valid task(s).[/dim]\n"
        )

    return valid


def get_task_paths_from_local(
    dataset_path: Path,
    task_names: list[str] | None = None,
    exclude_task_names: list[str] | None = None,
    n_tasks: int | None = None,
) -> list[Path]:
    """Get task paths from a local dataset directory using Harbor's DatasetConfig."""
    try:
        from harbor.models.job.config import DatasetConfig
    except ImportError:
        task_paths = [
            path
            for path in dataset_path.iterdir()
            if TaskPaths(path).is_valid(disable_verification=False)
        ]
        if task_names:
            task_paths = [
                path
                for path in task_paths
                if any(fnmatch(path.name, pattern) for pattern in task_names)
            ]
        if exclude_task_names:
            task_paths = [
                path
                for path in task_paths
                if not any(
                    fnmatch(path.name, pattern) for pattern in exclude_task_names
                )
            ]
        if n_tasks is not None:
            task_paths = task_paths[:n_tasks]
        return task_paths
    else:
        config = DatasetConfig(
            path=dataset_path,
            task_names=task_names,
            exclude_task_names=exclude_task_names,
            n_tasks=n_tasks,
        )
        task_configs = asyncio.run(config.get_task_configs())
        return [tc.path for tc in task_configs if tc.path is not None]


def get_task_paths_from_registry(
    dataset_name: str,
    version: str | None = None,
    task_names: list[str] | None = None,
    exclude_task_names: list[str] | None = None,
    n_tasks: int | None = None,
    quiet: bool = False,
) -> list[Path]:
    """Download a dataset from the Harbor registry and return local task paths."""
    # Parse name@version format
    if "@" in dataset_name and version is None:
        dataset_name, version = dataset_name.split("@", 1)

    if not quiet:
        console.print(
            f"[dim]Fetching dataset from registry: {dataset_name}@{version or 'latest'}[/dim]"
        )

    try:
        if "/" in dataset_name:
            from harbor.registry.client.package import PackageDatasetClient

            client = PackageDatasetClient()
            dataset_ref = f"{dataset_name}@{version or 'latest'}"
        else:
            from harbor.registry.client.factory import RegistryClientFactory

            client = RegistryClientFactory.create()
            dataset_ref = f"{dataset_name}@{version}" if version else dataset_name

        items = asyncio.run(client.download_dataset(dataset_ref))
        paths: list[Path] = [item.downloaded_path for item in items]

        if task_names:
            paths = [
                p for p in paths if any(fnmatch(p.name, pat) for pat in task_names)
            ]
        if exclude_task_names:
            paths = [
                p
                for p in paths
                if not any(fnmatch(p.name, pat) for pat in exclude_task_names)
            ]
        if n_tasks is not None:
            paths = paths[:n_tasks]

        if not quiet:
            console.print(f"[green]Downloaded {len(paths)} tasks[/green]")

        return paths

    except Exception as e:
        error_console.print(f"[red]Failed to download dataset:[/red] {e}")
        raise typer.Exit(1)


# =============================================================================
# Task Upload & Submit
# =============================================================================


def resolve_local_task_paths(
    *,
    path: Path | None,
    path_option: Path | None,
    dataset: str | None,
    task_names: list[str] | None,
    exclude_task_names: list[str] | None,
    n_tasks: int | None,
    quiet: bool,
) -> list[Path]:
    """Resolve a task-source flag bundle into a validated list of task paths.

    Shared by ``oddish run`` and ``oddish upload`` -- the first step of
    both commands is identical: decide which local task(s) the caller
    is targeting.

    Supports three input modes:

    - ``dataset`` (registry name, e.g. ``swebench@1.0``) -- downloads
      tasks via Harbor's registry client.
    - Positional ``path`` or ``--path`` pointing at a single Harbor
      task dir -- returns ``[path]``.
    - The same flags pointing at a *dataset directory* of task dirs --
      enumerates + filters via Harbor's ``DatasetConfig``.

    In all three cases every candidate is validated with
    :func:`validate_tasks` so callers can trust the returned paths are
    real Harbor tasks. Exits via ``typer.Exit(1)`` on validation
    failure or missing sources.
    """
    task_paths: list[Path] = []

    if dataset:
        if path or path_option:
            error_console.print(
                "[red]Provide either a path or --dataset, not both.[/red]"
            )
            raise typer.Exit(1)
        task_paths = get_task_paths_from_registry(
            dataset_name=dataset,
            task_names=task_names,
            exclude_task_names=exclude_task_names,
            n_tasks=n_tasks,
            quiet=quiet,
        )
    else:
        local_path = resolve_task_path(path, path_option)
        if not local_path:
            error_console.print(
                "[red]No task source specified.[/red]\n"
                "Provide a path or use --dataset/-d for registry datasets."
            )
            raise typer.Exit(1)

        if is_task_dir(local_path):
            task_paths = [local_path]
        else:
            task_paths = get_task_paths_from_local(
                dataset_path=local_path,
                task_names=task_names,
                exclude_task_names=exclude_task_names,
                n_tasks=n_tasks,
            )
            if not task_paths:
                error_console.print(
                    f"[red]No valid tasks found in {local_path}[/red]\n"
                    "A task directory must contain: task.toml, instruction.md, environment/, tests/"
                )
                raise typer.Exit(1)
            if not quiet:
                console.print(
                    f"[dim]Found {len(task_paths)} tasks in {local_path}[/dim]"
                )

    return validate_tasks(task_paths)


def compute_task_content_hash(task_path: Path) -> str:
    """Deterministic SHA-256 of a task directory's contents.

    Walks files in sorted order and hashes (relative_path, file_bytes) for each,
    so the result is independent of filesystem timestamps or tarball packaging.
    """
    hasher = hashlib.sha256()
    for file_path in sorted(task_path.rglob("*")):
        if file_path.is_file():
            rel = file_path.relative_to(task_path)
            hasher.update(str(rel).encode("utf-8"))
            hasher.update(file_path.read_bytes())
    return hasher.hexdigest()


def archive_task_dir(task_path: Path) -> Path:
    """Create a tarball of a task directory."""
    # Create tarball in temp directory
    tmpdir = tempfile.mkdtemp()
    tarball_path = Path(tmpdir) / f"{task_path.name}.tar.gz"

    # Favor fast uploads in CI/cloud flows over maximum compression.
    with tarfile.open(tarball_path, "w:gz", compresslevel=1) as tar:
        # Add contents of task_path to the tarball
        for item in task_path.iterdir():
            tar.add(item, arcname=item.name)

    return tarball_path


def _upload_to_presigned_url(
    url: str, tarball_path: Path, headers: dict[str, str]
) -> None:
    upload_headers = dict(headers)
    upload_headers.setdefault("Content-Length", str(tarball_path.stat().st_size))
    with httpx.Client(timeout=600.0, follow_redirects=True) as upload_client:
        response = upload_client.put(
            url,
            headers=upload_headers,
            content=tarball_path.read_bytes(),
        )
    if response.status_code not in {200, 201, 204}:
        error_console.print(
            f"[red]Failed to upload task directly to storage:[/red] {response.text}"
        )
        raise typer.Exit(1)


def upload_task(
    api_url: str,
    task_path: Path,
    *,
    register: bool = False,
    message: str | None = None,
    user: str | None = None,
    priority: str | None = None,
) -> dict:
    """Upload a task directory to the API.

    Returns the full upload response dict which includes ``task_id``,
    ``existing_task``, ``content_unchanged``, ``version``, etc.

    When ``register`` is True, asks the server to persist a TaskModel row
    immediately (used by ``oddish upload``). The legacy sweep path leaves
    this False so task-row creation still happens inside ``/tasks/sweep``.
    """
    try:
        validate_task_timeout_config(task_path)
    except TaskTimeoutValidationError as exc:
        error_console.print(f"[red]Invalid task timeout config:[/red] {exc}")
        raise typer.Exit(1) from exc

    content_hash = compute_task_content_hash(task_path)
    tarball_path = archive_task_dir(task_path)

    init_body: dict[str, object] = {
        "name": task_path.name,
        "content_hash": content_hash,
    }
    if message:
        init_body["message"] = message

    try:
        with httpx.Client(timeout=600.0, headers=get_auth_headers()) as client:
            init_response = client.post(
                f"{api_url}/tasks/upload/init",
                json=init_body,
            )

            if init_response.status_code != 200:
                error_console.print(
                    f"[red]Failed to initialize direct task upload:[/red] "
                    f"{init_response.text}"
                )
                raise typer.Exit(1)

            init_payload = cast(dict, init_response.json())
            if init_payload.get("content_unchanged"):
                return init_payload

            upload_url = init_payload.get("upload_url")
            if not isinstance(upload_url, str) or not upload_url:
                error_console.print(
                    "[red]Task upload initialization did not return a presigned upload URL.[/red]\n"
                    "Direct task uploads require S3-compatible storage."
                )
                raise typer.Exit(1)

            _upload_to_presigned_url(
                upload_url,
                tarball_path,
                cast(dict[str, str], init_payload.get("upload_headers") or {}),
            )
            complete_body: dict[str, object] = {
                "task_id": init_payload["task_id"],
                "name": init_payload["name"],
                "version": init_payload["version"],
                "content_hash": content_hash,
            }
            if message:
                complete_body["message"] = message
            if register:
                complete_body["register_task"] = True
            if user:
                complete_body["user"] = user
            if priority:
                complete_body["priority"] = priority
            response = client.post(
                f"{api_url}/tasks/upload/complete",
                json=complete_body,
            )

        if response.status_code != 200:
            error_console.print(f"[red]Failed to upload task:[/red] {response.text}")
            raise typer.Exit(1)

        return cast(dict, response.json())
    finally:
        shutil.rmtree(Path(tarball_path).parent, ignore_errors=True)


# Uploads are serialised to keep the presigned-PUT path simple and to avoid
# overwhelming the API's upload/init rate. Exported so callers
# (``oddish run`` / ``oddish upload``) can override if needed.
TASK_UPLOAD_CONCURRENCY = 1


def upload_tasks_with_progress(
    api_url: str,
    task_paths: list[Path],
    *,
    register: bool,
    message: str | None = None,
    user: str | None = None,
    priority: str | None = None,
    quiet: bool = False,
    json_output: bool = False,
    progress_label: str = "Uploading",
) -> list[dict]:
    """Upload a batch of task directories with a shared progress bar.

    Shared by ``oddish run`` (``register=False``-ish legacy mode -- the
    sweep endpoint creates the TaskModel) and ``oddish upload``
    (``register=True``, task becomes browsable immediately).

    Returns the upload response dicts in the same order as ``task_paths``.
    """
    if not task_paths:
        return []

    def _upload_one(task_path: Path) -> dict:
        return upload_task(
            api_url,
            task_path,
            register=register,
            message=message,
            user=user,
            priority=priority,
        )

    show_progress = not quiet and not json_output
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        disable=not show_progress,
    )

    results: list[dict] = []
    with progress:
        progress_task = progress.add_task(
            f"{progress_label} {len(task_paths)} tasks...", total=len(task_paths)
        )
        if len(task_paths) <= 1:
            for task_path in task_paths:
                results.append(_upload_one(task_path))
                progress.update(progress_task, advance=1)
        else:
            results_by_index: list[dict | None] = [None] * len(task_paths)
            max_workers = min(TASK_UPLOAD_CONCURRENCY, len(task_paths))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_index = {
                    executor.submit(_upload_one, task_path): index
                    for index, task_path in enumerate(task_paths)
                }
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    results_by_index[index] = future.result()
                    progress.update(progress_task, advance=1)
            results = [r for r in results_by_index if r is not None]

    return results


def _parse_key_value_pairs(pairs: list[str] | None) -> dict[str, str]:
    """Parse a list of 'key=value' strings into a dict."""
    if not pairs:
        return {}
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        result[key.strip()] = value.strip()
    return result


def submit_sweep(
    api_url: str,
    task_id: str,
    configs: list[dict],
    environment: EnvironmentType | None,
    user: str,
    priority: str,
    experiment_id: str | None,
    run_analysis: bool = False,
    github_username: str | None = None,
    tags: dict[str, str] | None = None,
    publish_experiment: bool | None = False,
    disable_verification: bool = False,
    override_cpus: int | None = None,
    override_memory_mb: int | None = None,
    override_gpus: int | None = None,
    override_storage_mb: int | None = None,
    force_build: bool | None = None,
    agent_env: list[str] | None = None,
    agent_kwargs: list[str] | None = None,
    artifact_paths: list[str] | None = None,
    append_to_task: bool = False,
    content_hash: str | None = None,
) -> dict:
    """Submit a task sweep to the API."""
    env_value = environment.value if environment else None

    if env_value is not None:
        for config in configs:
            config["environment"] = env_value

    harbor: dict = {}
    env_overrides: dict = {}
    if override_cpus is not None:
        env_overrides["override_cpus"] = override_cpus
    if override_memory_mb is not None:
        env_overrides["override_memory_mb"] = override_memory_mb
    if override_gpus is not None:
        env_overrides["override_gpus"] = override_gpus
    if override_storage_mb is not None:
        env_overrides["override_storage_mb"] = override_storage_mb
    if force_build is not None:
        env_overrides["force_build"] = force_build
    if env_overrides:
        harbor["environment"] = env_overrides
    if disable_verification:
        harbor["verifier"] = {"disable": True}
    if artifact_paths:
        harbor["artifacts"] = artifact_paths

    # CLI --ae/--ak flags apply to all configs as default agent overrides
    parsed_env = _parse_key_value_pairs(agent_env)
    parsed_kwargs = _parse_key_value_pairs(agent_kwargs)
    if parsed_env or parsed_kwargs:
        for config in configs:
            existing = config.get("agent_config") or {}
            if parsed_env:
                existing.setdefault("env", {}).update(parsed_env)
            if parsed_kwargs:
                existing.setdefault("kwargs", {}).update(parsed_kwargs)
            config["agent_config"] = existing

    payload: dict = {
        "task_id": task_id,
        "configs": configs,
        "user": user,
        "priority": priority,
        "run_analysis": run_analysis,
    }
    if experiment_id:
        payload["experiment_id"] = experiment_id
    if env_value is not None:
        payload["environment"] = env_value

    if github_username:
        payload["github_username"] = github_username
    if tags:
        payload["tags"] = tags
    payload["publish_experiment"] = publish_experiment
    if harbor:
        payload["harbor"] = harbor
    if append_to_task:
        payload["append_to_task"] = True
    if content_hash:
        payload["content_hash"] = content_hash

    with httpx.Client(
        timeout=TASK_SWEEP_TIMEOUT_SECONDS, headers=get_auth_headers()
    ) as client:
        response = client.post(f"{api_url}/tasks/sweep", json=payload)

    if response.status_code != 200:
        error_console.print(f"[red]Failed to submit task:[/red] {response.text}")
        raise typer.Exit(1)

    result: dict = response.json()
    return result


def get_experiment_share(api_url: str, experiment_id: str) -> dict | None:
    """Fetch experiment share metadata for a published experiment."""
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        response = client.get(f"{api_url}/experiments/{experiment_id}/share")
    if response.status_code != 200:
        return None
    return cast(dict, response.json())


# =============================================================================
# Trial Import (off-oddish Harbor run -> oddish trial rows)
# =============================================================================
#
# These helpers let ``oddish upload`` register trials executed outside of
# Oddish (e.g. a local ``harbor run``) as regular trial rows on an
# existing task. See ``oddish/core/trial_imports.py`` for the server side.


def is_harbor_job_dir(path: Path) -> bool:
    """Return True if *path* looks like a Harbor ``job_dir``.

    Harbor writes ``result.json`` at the top of every job dir and a
    per-trial ``result.json`` in each trial subdir. We only check the
    top-level one here -- the subdirs get filtered separately via
    ``JobScanner.list_trials``.
    """
    return path.is_dir() and (path / "result.json").is_file()


def is_harbor_jobs_dir(path: Path) -> bool:
    """Return True if *path* is a parent directory of multiple job dirs.

    Used to disambiguate ``./jobs`` (many harbor runs) from ``./jobs/my-run``
    (a single harbor run) when the user passes a single positional path
    to ``oddish upload``.
    """
    if not path.is_dir():
        return False
    if is_harbor_job_dir(path):
        return False
    # A jobs dir has at least one child that is itself a job dir.
    try:
        for child in path.iterdir():
            if is_harbor_job_dir(child):
                return True
    except OSError:
        return False
    return False


def discover_trial_entries(job_path: Path) -> list[tuple[str, str, Path]]:
    """Return ``(job_name, trial_name, trial_dir)`` tuples from *job_path*.

    Accepts either a single Harbor ``job_dir`` or a parent ``jobs_dir``
    with multiple job subdirs. Trial dirs without a ``result.json`` are
    skipped (Harbor writes one on every completed trial).
    """
    entries: list[tuple[str, str, Path]] = []

    if is_harbor_job_dir(job_path):
        scanner = JobScanner(job_path.parent)
        for trial_name in scanner.list_trials(job_path.name):
            entries.append((job_path.name, trial_name, job_path / trial_name))
        return entries

    scanner = JobScanner(job_path)
    for job_name in scanner.list_jobs():
        job_dir = job_path / job_name
        if not is_harbor_job_dir(job_dir):
            continue
        for trial_name in scanner.list_trials(job_name):
            entries.append((job_name, trial_name, job_dir / trial_name))

    return entries


def load_harbor_trial_result(trial_dir: Path) -> TrialResult | None:
    """Load the ``TrialResult`` stored at ``<trial_dir>/result.json``."""
    scanner = JobScanner(trial_dir.parent.parent)
    return scanner.get_trial_result(trial_dir.parent.name, trial_dir.name)


def detect_trajectory_in_dir(trial_dir: Path) -> bool:
    """Mirror ``oddish.workers.harbor_runner._detect_trajectory``."""
    if not trial_dir.exists():
        return False
    if any(trial_dir.rglob("trajectory.json")):
        return True
    if any(trial_dir.rglob("trajectory.jsonl")):
        return True
    return False


def trial_result_to_import_spec(
    trial_result: TrialResult,
    *,
    has_trajectory: bool,
) -> dict[str, Any]:
    """Convert a Harbor ``TrialResult`` to an ``ImportedTrialSpec`` payload.

    Per-trial equivalent of
    ``oddish.workers.harbor_runner._extract_outcome_from_job_result``.
    Multi-trial Harbor jobs (``-k > 1`` or multi-agent) become separate
    oddish trial rows.
    """
    agent_info = trial_result.agent_info
    model_info = agent_info.model_info

    reward: float | None = None
    if trial_result.verifier_result and trial_result.verifier_result.rewards:
        raw = trial_result.verifier_result.rewards.get("reward")
        if raw is not None:
            reward = float(raw)

    error_message: str | None = None
    if trial_result.exception_info is not None:
        exc = trial_result.exception_info
        error_message = (
            exc.exception_message or exc.exception_type or "Harbor execution error"
        )

    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    ctx = trial_result.agent_result
    if ctx is not None and not ctx.is_empty():
        input_tokens = ctx.n_input_tokens
        cache_tokens = ctx.n_cache_tokens
        output_tokens = ctx.n_output_tokens
        cost_usd = ctx.cost_usd

    phase_timing: dict[str, dict[str, Any]] = {}
    for phase in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        info = getattr(trial_result, phase, None)
        if info is None or info.started_at is None or info.finished_at is None:
            continue
        phase_timing[phase] = {
            "started_at": info.started_at.isoformat(),
            "finished_at": info.finished_at.isoformat(),
            "duration_sec": round(
                (info.finished_at - info.started_at).total_seconds(), 2
            ),
        }

    # SUCCESS iff the verifier produced a reward (partial counts as
    # SUCCESS in oddish -- matches the live semantics). Otherwise the
    # execution hit an error and the row is FAILED.
    status = "success" if reward is not None else "failed"

    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.isoformat()

    return {
        "agent": agent_info.name,
        "model": model_info.name if model_info is not None else None,
        "status": status,
        "reward": reward,
        "error_message": error_message,
        "harbor_stage": "completed",
        "input_tokens": input_tokens,
        "cache_tokens": cache_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "phase_timing": phase_timing or None,
        "has_trajectory": has_trajectory,
        "started_at": _iso(trial_result.started_at),
        "finished_at": _iso(trial_result.finished_at),
        "external_trial_id": str(trial_result.id),
    }


def _tar_trial_dir(trial_dir: Path) -> Path:
    """Tarball a Harbor trial's artifacts for upload via presigned PUT.

    Mirrors the live Oddish S3 layout (see
    ``StorageClient.upload_trial_results`` in
    ``oddish/src/oddish/workers/queue/trial_handler.py``) so the file
    viewer, ``/trials/<id>/result``, and trajectory lookups return
    identical shapes for imported and live trials.

    The layout written under ``tasks/<task_id>/trials/<trial_id>/`` is:

        <root>/
            config.json            # JOB-level config (from job_dir root)
            job.log
            modal-output.log       # if the job produced one
            result.json            # JOB-level result (JobResult blob)
            <trial_name>/          # trial subdir nested one level
                config.json        # TRIAL-level config
                result.json        # TRIAL-level result
                trial.log
                verifier/
                agent/trajectory.json
                ...

    Top-level sibling trial subdirs from the same Harbor job are
    excluded on purpose -- each imported trial gets its own S3 prefix
    and shouldn't drag in its sibling trials' logs.
    """
    tmpdir = tempfile.mkdtemp(prefix="oddish-trial-import-")
    tarball_path = Path(tmpdir) / f"{trial_dir.name}.tar.gz"
    job_dir = trial_dir.parent
    with tarfile.open(tarball_path, "w:gz", compresslevel=1) as tar:
        # 1. Add the job dir's top-level FILES only (config, logs,
        #    job-level result.json). Skipping subdirectories here
        #    omits sibling trials' data from this trial's archive.
        if job_dir.exists():
            for item in job_dir.iterdir():
                if item.is_file():
                    tar.add(item, arcname=item.name)
        # 2. Add the trial's own subdir nested under its trial_name so
        #    ``<prefix>/<trial_name>/agent/trajectory.json`` etc. line
        #    up with the live path's ``_trajectory_candidate_keys``.
        tar.add(trial_dir, arcname=trial_dir.name)
    return tarball_path


def _call_trial_import_init(
    api_url: str,
    *,
    task_id: str,
    experiment_id: str | None,
    trial_payload: dict[str, Any],
    upload_artifacts: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_id": task_id,
        "trial": trial_payload,
        "upload_artifacts": upload_artifacts,
    }
    if experiment_id:
        body["experiment_id"] = experiment_id
    with httpx.Client(timeout=120.0, headers=get_auth_headers()) as client:
        resp = client.post(f"{api_url}/trials/import/init", json=body)
    if resp.status_code != 200:
        error_console.print(
            f"[red]Failed to initialize trial import:[/red] {resp.text}"
        )
        raise typer.Exit(1)
    return cast(dict[str, Any], resp.json())


def _call_trial_import_complete(api_url: str, *, trial_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=600.0, headers=get_auth_headers()) as client:
        resp = client.post(
            f"{api_url}/trials/import/complete",
            json={"trial_id": trial_id},
        )
    if resp.status_code != 200:
        error_console.print(f"[red]Failed to finalize trial import:[/red] {resp.text}")
        raise typer.Exit(1)
    return cast(dict[str, Any], resp.json())


def import_trial(
    api_url: str,
    *,
    task_id: str,
    experiment_id: str | None,
    trial_dir: Path,
    upload_artifacts: bool,
) -> dict[str, Any]:
    """Import a single Harbor trial dir into Oddish.

    Returns the init response augmented with ``files_extracted`` from
    the complete step (0 when ``upload_artifacts`` is False).
    """
    trial_result = load_harbor_trial_result(trial_dir)
    if trial_result is None:
        raise typer.Exit(code=2)

    has_trajectory = detect_trajectory_in_dir(trial_dir)
    spec_payload = trial_result_to_import_spec(
        trial_result, has_trajectory=has_trajectory
    )

    init = _call_trial_import_init(
        api_url,
        task_id=task_id,
        experiment_id=experiment_id,
        trial_payload=spec_payload,
        upload_artifacts=upload_artifacts,
    )
    trial_id = init["trial_id"]

    if upload_artifacts:
        upload_url = init.get("upload_url")
        if isinstance(upload_url, str) and upload_url:
            tarball_path = _tar_trial_dir(trial_dir)
            try:
                _upload_to_presigned_url(
                    upload_url,
                    tarball_path,
                    cast(dict[str, str], init.get("upload_headers") or {}),
                )
            finally:
                shutil.rmtree(Path(tarball_path).parent, ignore_errors=True)
            complete = _call_trial_import_complete(api_url, trial_id=trial_id)
            init["files_extracted"] = complete.get("files_extracted", 0)
        else:
            init["files_extracted"] = 0
    else:
        init["files_extracted"] = 0

    return init


def get_task_summary(api_url: str, task_id: str) -> dict | None:
    """Fetch a task summary by ID."""
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        response = client.get(f"{api_url}/tasks/{task_id}")
    if response.status_code != 200:
        return None
    return cast(dict, response.json())


# =============================================================================
# Config File Loading
# =============================================================================


def load_sweep_config(config_path: Path) -> dict:
    """Load and validate a sweep config file (YAML or JSON).

    Expected format::

        agents:
          - name: claude-code
            model_name: claude-sonnet-4-5
            n_trials: 4
            env:                        # optional: agent env vars
              CUSTOM_VAR: "value"
            kwargs:                     # optional: agent kwargs
              max_thinking_tokens: 8000

          - name: codex
            model_name: gpt-5.2
            n_trials: 3
            timeout_minutes: 120        # optional: per-agent timeout

        # Task source (pick one):
        path: ./my-task                 # local task or dataset directory
        dataset: swebench@1.0           # registry dataset

        # Optional filtering (Harbor-compatible):
        task_names: ["task-*"]          # glob patterns to include
        exclude_task_names: ["*-slow"]  # glob patterns to exclude
        n_tasks: 10                     # max tasks to run

        # Optional fields:
        environment: daytona            # execution environment
        priority: low
        experiment_id: exp_123
    """
    if not config_path.exists():
        error_console.print(f"[red]Config file not found:[/red] {config_path}")
        raise typer.Exit(1)

    try:
        content = config_path.read_text()
        if config_path.suffix in (".yaml", ".yml"):
            config = yaml.safe_load(content)
        elif config_path.suffix == ".json":
            config = json.loads(content)
        else:
            # Try YAML first, then JSON
            try:
                config = yaml.safe_load(content)
            except Exception:
                config = json.loads(content)
    except Exception as e:
        error_console.print(f"[red]Failed to parse config file:[/red] {e}")
        raise typer.Exit(1)

    # Validate required fields
    if "agents" not in config or not config["agents"]:
        error_console.print(
            "[red]Config must have 'agents' list with at least one entry[/red]"
        )
        raise typer.Exit(1)

    # Normalize and validate agent entries using Harbor's AgentConfig
    if "timeout_minutes" in config:
        error_console.print(
            "[red]Top-level 'timeout_minutes' is no longer supported.[/red]\n"
            "Declare explicit timeouts in task.toml instead."
        )
        raise typer.Exit(1)

    normalized_agents = []
    for i, agent_entry in enumerate(config["agents"]):
        agent_data = {
            "name": agent_entry.get("name"),
            "model_name": agent_entry.get("model_name"),
        }

        if not agent_data["name"]:
            error_console.print(f"[red]Agent entry {i + 1} missing 'name' field[/red]")
            raise typer.Exit(1)
        if agent_data["model_name"] is None:
            error_console.print(
                f"[red]Agent entry {i + 1} missing 'model_name' field[/red]"
            )
            raise typer.Exit(1)

        # Validate using Harbor's AgentConfig model (validates name, model_name, etc.)
        try:
            harbor_config = AgentConfig.model_validate(agent_data)
        except Exception as e:
            error_console.print(
                f"[red]Invalid agent config at entry {i + 1}:[/red] {e}"
            )
            raise typer.Exit(1)

        if "n_concurrent" in agent_entry or "concurrency" in agent_entry:
            error_console.print(
                f"[red]Agent entry {i + 1} includes 'n_concurrent', which is no longer supported.[/red]\n"
                "Set provider concurrency when starting the API (e.g. --n-concurrent)."
            )
            raise typer.Exit(1)

        entry: dict = {
            "agent": harbor_config.name,
            "model": harbor_config.model_name,
            "n_trials": agent_entry.get("n_trials", 1),
        }

        agent_config_overrides: dict = {}
        if agent_entry.get("env"):
            agent_config_overrides["env"] = agent_entry["env"]
        if agent_entry.get("kwargs"):
            agent_config_overrides["kwargs"] = agent_entry["kwargs"]
        if agent_config_overrides:
            entry["agent_config"] = agent_config_overrides

        if "timeout_minutes" in agent_entry:
            error_console.print(
                f"[red]Agent entry {i + 1} includes 'timeout_minutes', which is no longer supported.[/red]\n"
                "Declare explicit timeouts in task.toml instead."
            )
            raise typer.Exit(1)
        normalized_agents.append(entry)

    config["agents"] = normalized_agents
    return cast(dict, config)


# =============================================================================
# Status Formatting
# =============================================================================


def format_task_status(status: str) -> str:
    """Format task status with color coding."""
    style_map = {
        "pending": ("dim", "pending"),
        "running": ("blue", "running"),
        "analyzing": ("cyan", "analyzing"),
        "verdict_pending": ("magenta", "verdict"),
        "completed": ("green", "completed"),
        "failed": ("red", "failed"),
    }
    style, label = style_map.get(status.lower(), ("white", status))
    return f"[{style}]{label}[/{style}]"


def format_trial_status(status: str, harbor_stage: str | None = None) -> str:
    """Format trial status with optional harbor stage."""
    style_map = {
        "pending": "dim",
        "queued": "yellow",
        "running": "blue",
        "retrying": "yellow",
        "success": "green",
        "failed": "red",
    }
    style = style_map.get(status.lower(), "white")

    if status.lower() == "running" and harbor_stage:
        # Show harbor stage for running trials
        return f"[{style}]{harbor_stage}[/{style}]"
    return f"[{style}]{status}[/{style}]"


def format_verdict_status(verdict_status: str) -> str:
    """Format verdict status with color coding."""
    style_map = {
        "pending": "[dim]pending[/dim]",
        "queued": "[yellow]queued[/yellow]",
        "running": "[blue]running[/blue]",
        "success": "[green]done[/green]",
        "failed": "[red]failed[/red]",
    }
    return style_map.get(verdict_status.lower(), verdict_status)


def _summarize_experiment_tasks(tasks: list[dict]) -> dict:
    total_tasks = len(tasks)
    task_completed = sum(1 for t in tasks if t.get("status") in ("completed", "failed"))
    task_running = sum(1 for t in tasks if t.get("status") == "running")
    task_pending = total_tasks - task_completed - task_running

    total_trials = sum(t.get("total", 0) or 0 for t in tasks)
    completed_trials = sum(t.get("completed", 0) or 0 for t in tasks)
    failed_trials = sum(t.get("failed", 0) or 0 for t in tasks)

    reward_success = sum(t.get("reward_success", 0) or 0 for t in tasks)
    reward_total = sum(t.get("reward_total", 0) or 0 for t in tasks)

    return {
        "total_tasks": total_tasks,
        "task_completed": task_completed,
        "task_running": task_running,
        "task_pending": task_pending,
        "total_trials": total_trials,
        "completed_trials": completed_trials,
        "failed_trials": failed_trials,
        "reward_success": reward_success,
        "reward_total": reward_total,
    }


def _build_experiment_table(experiment_id: str, tasks: list[dict]) -> Table:
    experiment_name = tasks[0].get("experiment_name") if tasks else None
    title = f"Experiment: {experiment_id}"
    if experiment_name:
        title = f"{title} ({experiment_name})"

    table = Table(title=title)
    table.add_column("Task", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Progress")
    table.add_column("Rewards", justify="center")
    table.add_column("Verdict", justify="center")

    for task in tasks:
        reward_total = task.get("reward_total")
        reward_success = task.get("reward_success")
        if reward_total:
            reward_display = f"{reward_success}/{reward_total}"
        else:
            reward_display = "-"

        verdict_status = task.get("verdict_status")
        verdict_display = (
            format_verdict_status(verdict_status) if verdict_status else "-"
        )

        table.add_row(
            task["id"],
            format_task_status(task.get("status", "unknown")),
            task.get("progress") or "-",
            reward_display,
            verdict_display,
        )

    summary = _summarize_experiment_tasks(tasks)
    table.add_section()
    summary_parts = [
        f"[bold]{summary['task_completed']}/{summary['total_tasks']}[/bold] tasks done"
    ]
    if summary["task_running"]:
        summary_parts.append(f"[blue]{summary['task_running']} running[/blue]")
    if summary["task_pending"]:
        summary_parts.append(f"[dim]{summary['task_pending']} pending[/dim]")
    if summary["failed_trials"]:
        summary_parts.append(f"[red]{summary['failed_trials']} failed trials[/red]")
    if summary["reward_total"]:
        summary_parts.append(
            f"[green]{summary['reward_success']}✓[/green]/"
            f"[red]{summary['reward_total'] - summary['reward_success']}✗[/red]"
        )

    table.add_row("", ", ".join(summary_parts), "", "", "")
    return table


def get_experiment_tasks(api_url: str, experiment_id: str) -> list[dict] | None:
    """Fetch all tasks for an experiment by ID."""
    try:
        with httpx.Client(timeout=10.0, headers=get_auth_headers()) as client:
            response = client.get(
                f"{api_url}/tasks", params={"experiment_id": experiment_id}
            )
    except Exception as e:
        error_console.print(f"[red]Failed to connect to API:[/red] {e}")
        return None

    if response.status_code != 200:
        error_console.print(f"[red]Failed to get experiment:[/red] {response.text}")
        return None

    return cast(list[dict], response.json())


def print_experiment_status(api_url: str, experiment_id: str) -> bool:
    """Print an experiment status summary. Returns True if found."""
    tasks = get_experiment_tasks(api_url, experiment_id)
    if tasks is None:
        return False

    if not tasks:
        console.print(
            f"[yellow]No tasks found for experiment:[/yellow] {experiment_id}"
        )
        return False

    summary = _summarize_experiment_tasks(tasks)
    console.print(f"[bold]Experiment:[/bold] {experiment_id}")
    experiment_name = tasks[0].get("experiment_name")
    if experiment_name:
        console.print(f"[bold]Name:[/bold] {experiment_name}")
    console.print(
        f"[bold]Tasks:[/bold] {summary['total_tasks']} total"
        f" ({summary['task_running']} running, {summary['task_completed']} done)"
    )
    console.print(
        f"[bold]Trials:[/bold] {summary['completed_trials']}/{summary['total_trials']} completed"
    )
    if summary["reward_total"]:
        console.print(
            f"[bold]Rewards:[/bold] {summary['reward_success']}/{summary['reward_total']} passed"
        )

    console.print()
    console.print(_build_experiment_table(experiment_id, tasks))
    return True


def watch_experiment(api_url: str, experiment_id: str) -> None:
    """Watch an experiment until all tasks complete."""
    headers = get_auth_headers()
    with Live(console=console, refresh_per_second=2) as live:
        while True:
            try:
                with httpx.Client(timeout=10.0, headers=headers) as client:
                    response = client.get(
                        f"{api_url}/tasks", params={"experiment_id": experiment_id}
                    )

                if response.status_code != 200:
                    live.update(f"[red]Failed to get status:[/red] {response.text}")
                    break

                tasks = cast(list[dict], response.json())
                if not tasks:
                    live.update(
                        f"[yellow]No tasks found for experiment:[/yellow] {experiment_id}"
                    )
                    break

                live.update(_build_experiment_table(experiment_id, tasks))

                if all(t.get("status") in ("completed", "failed") for t in tasks):
                    break

                time.sleep(2)
            except Exception as e:
                live.update(f"[red]Error:[/red] {e}")
                time.sleep(2)


# =============================================================================
# Task Results & Watching
# =============================================================================


def print_final_results(result: dict) -> None:
    """Print a final summary table when task completes (Harbor-style output)."""
    console.print()

    # Build results table
    table = Table(title=f"Results: {result['id']}")
    table.add_column("Trial", style="cyan", no_wrap=True)
    table.add_column("Agent")
    table.add_column("Model")
    table.add_column("Status")
    table.add_column("Reward", justify="right")

    # Track stats
    total = 0
    succeeded = 0
    failed = 0
    rewards = []

    for trial in result.get("trials", []):
        total += 1
        status = trial["status"]

        if status == "success":
            succeeded += 1
            status_str = "[green]success[/green]"
        elif status == "failed":
            failed += 1
            status_str = "[red]failed[/red]"
        elif status == "running":
            status_str = "[blue]running[/blue]"
        else:
            status_str = f"[dim]{status}[/dim]"

        reward = trial.get("reward")
        if reward is not None:
            reward_value = float(reward)
            rewards.append(reward_value)
            reward_str = format_reward_value(reward_value)
        else:
            reward_str = "-"

        # Shorten trial ID for display
        short_id = trial["id"].split("-")[-1] if "-" in trial["id"] else trial["id"][:8]

        table.add_row(
            short_id,
            trial["agent"],
            trial.get("model") or "-",
            status_str,
            reward_str,
        )

    console.print(table)

    # Print summary line
    console.print()
    summary_parts = [f"[bold]{total} trials[/bold]"]
    if succeeded:
        summary_parts.append(f"[green]{succeeded} succeeded[/green]")
    if failed:
        summary_parts.append(f"[red]{failed} failed[/red]")
    if rewards:
        avg_reward = sum(rewards) / len(rewards)
        summary_parts.append(f"avg score: [cyan]{avg_reward:.2f}[/cyan]")

    console.print("  " + " | ".join(summary_parts))
    console.print()


def watch_task(
    api_url: str,
    task_id: str,
    experiment_id: str | None = None,
    trial_ids: Iterable[str] | None = None,
) -> dict | None:
    """Watch a task until completion. Returns the final result.

    When *experiment_id* is given, only trials belonging to that experiment
    are displayed (others are hidden from the table and summary counts).

    When *trial_ids* is given, only trials whose ``id`` is in that set are
    shown. This is useful when appending trials to an existing task and the
    caller only wants to monitor the freshly-submitted trials.
    """
    final_result = None
    headers = get_auth_headers()
    trial_id_filter = set(trial_ids) if trial_ids is not None else None
    with Live(console=console, refresh_per_second=2) as live:
        while True:
            try:
                with httpx.Client(timeout=10.0, headers=headers) as client:
                    response = client.get(f"{api_url}/tasks/{task_id}")

                if response.status_code != 200:
                    live.update(f"[red]Failed to get status:[/red] {response.text}")
                    break

                result = cast(dict, response.json())
                final_result = result

                all_trials = result.get("trials", [])
                if trial_id_filter is not None:
                    all_trials = [
                        t for t in all_trials if t.get("id") in trial_id_filter
                    ]
                elif experiment_id:
                    all_trials = [
                        t for t in all_trials if t.get("experiment_id") == experiment_id
                    ]

                task_status = result.get("status", "unknown")
                task_status_display = format_task_status(task_status)

                # Build status table
                table = Table(title=f"Task: {task_id}  {task_status_display}")
                table.add_column("#", style="cyan", justify="right")
                table.add_column("Agent")
                table.add_column("Model")
                table.add_column("Status")
                table.add_column("Reward", justify="center")

                for trial in all_trials:
                    status = trial["status"]
                    harbor_stage = trial.get("harbor_stage")
                    status_display = format_trial_status(status, harbor_stage)

                    reward = trial.get("reward")
                    reward_str = format_reward_value(
                        float(reward) if reward is not None else None
                    )

                    table.add_row(
                        trial["id"].split("-")[-1],  # Just the index
                        trial["agent"],
                        trial.get("model") or "-",
                        status_display,
                        reward_str,
                    )

                # Add summary row
                total = len(all_trials)
                completed = sum(1 for t in all_trials if t.get("status") == "success")
                failed = sum(1 for t in all_trials if t.get("status") == "failed")

                rewards = [
                    float(t["reward"])
                    for t in all_trials
                    if t.get("reward") is not None
                ]
                reward_pass = sum(1 for reward in rewards if reward == 1)
                reward_fail = sum(1 for reward in rewards if reward == 0)
                reward_partial = sum(1 for reward in rewards if 0 < reward < 1)

                table.add_section()
                summary_parts = [f"[bold]{completed}/{total}[/bold] done"]
                if failed > 0:
                    summary_parts.append(f"[red]{failed} failed[/red]")
                if rewards:
                    summary_parts.append(
                        f"avg [cyan]{sum(rewards) / len(rewards):.2f}[/cyan]"
                    )
                if reward_pass > 0 or reward_fail > 0 or reward_partial > 0:
                    reward_summary = []
                    if reward_pass > 0:
                        reward_summary.append(f"[green]{reward_pass}✓[/green]")
                    if reward_partial > 0:
                        reward_summary.append(f"[yellow]{reward_partial}~[/yellow]")
                    if reward_fail > 0:
                        reward_summary.append(f"[red]{reward_fail}✗[/red]")
                    summary_parts.append("/".join(reward_summary))

                table.add_row("", ", ".join(summary_parts), "", "", "")

                # Show verdict status if in later pipeline stages
                if task_status in ("analyzing", "verdict_pending", "completed"):
                    verdict_status = result.get("verdict_status")
                    if verdict_status:
                        verdict_display = {
                            "pending": "[dim]pending[/dim]",
                            "queued": "[yellow]queued[/yellow]",
                            "running": "[blue]running[/blue]",
                            "success": "[green]done[/green]",
                            "failed": "[red]failed[/red]",
                        }.get(verdict_status.lower(), verdict_status)
                        table.add_row("", f"Verdict: {verdict_display}", "", "", "")

                live.update(table)

                # Check if done
                if trial_id_filter is not None or experiment_id:
                    terminal = {"success", "failed", "cancelled"}
                    if all_trials and all(
                        t.get("status") in terminal for t in all_trials
                    ):
                        break
                elif task_status in ("completed", "failed"):
                    break

                time.sleep(2)

            except Exception as e:
                live.update(f"[red]Error:[/red] {e}")
                time.sleep(2)

    return final_result

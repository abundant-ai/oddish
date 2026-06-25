from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import TaskConfig as HarborTaskConfig

from oddish.cli._concurrency import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MIN_CONCURRENCY,
    SUBMIT_CONCURRENCY_ENV_VAR,
    map_with_adaptive_concurrency,
    resolve_submit_concurrency,
)
from oddish.cli.api import (
    build_sweep_payload,
    get_experiment_share,
    get_task_summary,
    load_sweep_config,
    post_sweep_payload,
    print_final_results,
    resolve_local_task_paths,
    submit_sweep_batch,
    upload_tasks_with_progress,
    watch_task,
)
from oddish.cli.config import (
    error_console,
    get_api_url,
    get_dashboard_url,
    is_modal_api_url,
    require_api_key,
)
from oddish.experiment import generate_experiment_name

console = Console()


def _task_config_requests_gpu(task_path: Path) -> bool:
    config_path = task_path / "task.toml"
    try:
        task_config = HarborTaskConfig.model_validate_toml(config_path.read_text())
    except Exception:
        return False
    # ``gpus`` is optional in the task schema (e.g. schema_version 1.2 leaves
    # it unset -> None); treat an absent value as 0 GPUs instead of crashing
    # on ``None > 0``.
    return (task_config.environment.gpus or 0) > 0


def _read_harbor_manifest(*candidates: Path | None) -> dict[str, str] | None:
    """Read a ``[harbor] source/ref`` table from an ``oddish.toml`` sidecar.

    Checks each candidate task path (dir -> ``<dir>/oddish.toml``, file ->
    sibling); the first sidecar found wins. This is the manifest precedence
    layer (below CLI/env, above the locked default) and the escape hatch for
    refs/URLs containing a literal ``@``. Returns None when there's no sidecar.
    """
    import tomllib

    for candidate in candidates:
        if candidate is None:
            continue
        base = candidate if candidate.is_dir() else candidate.parent
        toml_path = base / "oddish.toml"
        if not toml_path.is_file():
            continue
        try:
            data = tomllib.loads(toml_path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return None
        harbor_table = data.get("harbor")
        if isinstance(harbor_table, dict):
            return {
                key: str(harbor_table[key])
                for key in ("source", "ref")
                if harbor_table.get(key) is not None
            }
    return None


def _default_cloud_environment_for_task(
    task_path: Path | None,
    *,
    override_gpus: int | None,
) -> EnvironmentType:
    if override_gpus is not None:
        return EnvironmentType.MODAL if override_gpus > 0 else EnvironmentType.DAYTONA
    if task_path is not None and _task_config_requests_gpu(task_path):
        return EnvironmentType.MODAL
    return EnvironmentType.DAYTONA


def _map_batch_sweep_results(
    batch_results: list,
    submit_targets: list,
) -> tuple[list[dict | None], int, list[str]]:
    """Map per-item batch sweep results onto the submitted task order.

    Returns ``(results_by_index, total_trials, failed_target_ids)``: each
    successful item's task payload is placed at its request index, and failures
    are reported by task id (best-effort). A malformed (non-dict) item raises
    ``typer.Exit(1)`` -- the batch may already have committed, so surface the bad
    response rather than crash or guess.
    """
    results_by_index: list[dict | None] = [None] * len(submit_targets)
    total_trials = 0
    failed_targets: list[str] = []
    for item in batch_results:
        if not isinstance(item, dict):
            error_console.print(
                "[red]Batch task submission returned a malformed item.[/red]\n"
                "[yellow]Not retrying per task to avoid duplicate trials.[/yellow]"
            )
            raise typer.Exit(1)
        idx = item.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(submit_targets):
            continue
        task_resp = item.get("task")
        if item.get("success") and isinstance(task_resp, dict):
            results_by_index[idx] = task_resp
            total_trials += task_resp.get("trials_count", 0)
        else:
            failed_targets.append(submit_targets[idx][0])
            error_console.print(
                f"[red]Failed to submit task {submit_targets[idx][0]}:[/red] "
                f"{item.get('error')}"
            )
    return results_by_index, total_trials, failed_targets


def run(
    path: Annotated[
        Optional[Path],
        typer.Argument(
            help="Path to task or dataset directory",
        ),
    ] = None,
    path_option: Annotated[
        Optional[Path],
        typer.Option(
            "--path",
            "-p",
            help="Path to task or dataset directory (Harbor-compatible flag)",
        ),
    ] = None,
    dataset: Annotated[
        Optional[str],
        typer.Option(
            "--dataset",
            "-d",
            help="Registry dataset (e.g., 'swebench@1.0' or 'swebench' for latest)",
        ),
    ] = None,
    existing_task_id: Annotated[
        Optional[str],
        typer.Option(
            "--task",
            help="Append trials to an existing task ID instead of uploading task files",
        ),
    ] = None,
    config: Annotated[
        Optional[Path],
        typer.Option(
            "--config",
            "-c",
            help="Config file (YAML/JSON) for complex sweeps with multiple agents/models",
        ),
    ] = None,
    agent: Annotated[
        Optional[str],
        typer.Option(
            "--agent",
            "-a",
            help="Agent to run (use --config for multiple agents)",
        ),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option(
            "--model",
            "-m",
            help="Model to use (optional)",
        ),
    ] = None,
    harbor: Annotated[
        Optional[str],
        typer.Option(
            "--harbor",
            help=(
                "Override Harbor source/ref for this run, e.g. 'main', "
                "'v0.13.1', a 40-hex SHA, 'org/repo@ref', or a git URL@ref. "
                "A bare 'org/repo' (no '@') is a BRANCH on the default fork, "
                "not another repo — use 'org/repo@ref' or a full URL to point "
                "elsewhere. For a ref or URL containing a literal '@', use the "
                "oddish.toml [harbor] source/ref escape hatch. "
                "Default: the locked fork commit. (env: ODDISH_HARBOR)"
            ),
        ),
    ] = None,
    n_trials: Annotated[
        int,
        typer.Option(
            "--n-trials",
            help="Number of trials per task (Oddish-specific; Harbor uses -k for retries)",
        ),
    ] = 1,
    max_trial_attempts: Annotated[
        Optional[int],
        typer.Option(
            "--max-trial-attempts",
            min=1,
            help=(
                "Override maximum Oddish attempts per trial, including the initial run. "
                "Can also be set in sweep config as max_trial_attempts."
            ),
        ),
    ] = None,
    # Harbor-compatible filtering options
    task_names: Annotated[
        Optional[list[str]],
        typer.Option(
            "-t",
            "--task-name",
            help="Task name filter (glob pattern, can be used multiple times)",
        ),
    ] = None,
    exclude_task_names: Annotated[
        Optional[list[str]],
        typer.Option(
            "-x",
            "--exclude-task-name",
            help="Task name to exclude (glob pattern, can be used multiple times)",
        ),
    ] = None,
    n_tasks: Annotated[
        Optional[int],
        typer.Option(
            "-l",
            "--n-tasks",
            help="Maximum number of tasks to run (applied after filters)",
        ),
    ] = None,
    environment: Annotated[
        Optional[EnvironmentType],
        typer.Option(
            "--env",
            "-e",
            help=(
                "Execution environment (docker, daytona, e2b, modal, runloop, gke). "
                "Defaults: daytona for CPU-only hosted tasks, modal for GPU hosted "
                "tasks, docker otherwise."
            ),
        ),
    ] = None,
    priority: Annotated[
        str,
        typer.Option(
            "--priority",
            "-P",
            help="Priority (low or high)",
        ),
    ] = "low",
    experiment_id: Annotated[
        Optional[str],
        typer.Option(
            "--experiment",
            "-E",
            help="Experiment ID or name (creates if not found, omit to auto-generate)",
        ),
    ] = None,
    user: Annotated[
        Optional[str],
        typer.Option(
            "--user",
            "-u",
            help="Override the task author (defaults to your authenticated identity).",
        ),
    ] = None,
    github_user: Annotated[
        Optional[str],
        typer.Option(
            "--github-user",
            "-G",
            help="GitHub username to attribute this task to. Used for CI attribution.",
        ),
    ] = None,
    github_meta: Annotated[
        Optional[str],
        typer.Option(
            "--github-meta",
            help="JSON metadata to associate with this task (e.g. PR info).",
        ),
    ] = None,
    link: Annotated[
        Optional[str],
        typer.Option(
            "--link",
            help="URL to associate with this task (e.g. PR, issue, CI run)",
        ),
    ] = None,
    publish: Annotated[
        bool,
        typer.Option(
            "--publish/--no-publish",
            help="Publish experiment for public read-only access",
        ),
    ] = False,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch/--no-watch",
            "-w",
            help="Watch task progress until completion (default: enabled)",
        ),
    ] = True,
    background: Annotated[
        bool,
        typer.Option(
            "--background",
            "--async",
            "-b",
            help="Submit task and return immediately (don't wait for completion)",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Suppress infrastructure startup logs",
        ),
    ] = False,
    run_analysis: Annotated[
        bool,
        typer.Option(
            "--run-analysis",
            help="Run LLM analysis on each trial and compute task verdict",
        ),
    ] = False,
    run_probe: Annotated[
        bool,
        typer.Option(
            "--run-probe",
            help="Auto-enqueue a probe trial for this task's version (off by default)",
        ),
    ] = False,
    disable_verification: Annotated[
        bool,
        typer.Option(
            "--disable-verification/--enable-verification",
            help="Disable task verification (skip running tests)",
        ),
    ] = False,
    override_cpus: Annotated[
        Optional[int],
        typer.Option(
            "--override-cpus",
            help="Override the number of CPUs for the environment",
        ),
    ] = None,
    override_memory_mb: Annotated[
        Optional[int],
        typer.Option(
            "--override-memory-mb",
            help="Override the memory (in MB) for the environment",
        ),
    ] = None,
    override_gpus: Annotated[
        Optional[int],
        typer.Option(
            "--override-gpus",
            help="Override the number of GPUs for the environment",
        ),
    ] = None,
    override_storage_mb: Annotated[
        Optional[int],
        typer.Option(
            "--override-storage-mb",
            help="Override the storage (in MB) for the environment",
        ),
    ] = None,
    force_build: Annotated[
        Optional[bool],
        typer.Option(
            "--force-build/--no-force-build",
            help="Force rebuild the environment Docker image",
        ),
    ] = None,
    environment_kwargs: Annotated[
        Optional[list[str]],
        typer.Option(
            "--environment-kwarg",
            "--harbor-environment-kwarg",
            help=(
                "Harbor environment kwarg in KEY=VALUE format, e.g. "
                "agent_tools_image=ghcr.io/org/harbor-agent-tools:tag "
                "(can be used multiple times)"
            ),
        ),
    ] = None,
    force_new_version: Annotated[
        bool,
        typer.Option(
            "--force-new-version",
            help=(
                "Allocate a new task version even when the local content is "
                "unchanged from the latest existing version. Useful when "
                "appending trials with a different run_analysis setting."
            ),
        ),
    ] = False,
    agent_env: Annotated[
        Optional[list[str]],
        typer.Option(
            "--ae",
            "--agent-env",
            help="Environment variable for the agent in KEY=VALUE format (can be used multiple times)",
        ),
    ] = None,
    agent_kwargs: Annotated[
        Optional[list[str]],
        typer.Option(
            "--ak",
            "--agent-kwarg",
            help="Agent kwarg in key=value format (can be used multiple times)",
        ),
    ] = None,
    artifact_paths: Annotated[
        Optional[list[str]],
        typer.Option(
            "--artifact",
            help="Environment path to download as an artifact after the trial (can be used multiple times)",
        ),
    ] = None,
    registry_login: Annotated[
        Optional[list[str]],
        typer.Option(
            "--registry-login",
            help=(
                "Per-run container-registry login as comma-separated pairs "
                "'username=USER,token=TOKEN[,registry=docker.io]' (repeatable). "
                "Docker Hub creds can also be set via "
                "ODDISH_DOCKERHUB_USERNAME / ODDISH_DOCKERHUB_TOKEN."
            ),
        ),
    ] = None,
    retry: Annotated[
        bool,
        typer.Option(
            "--retry",
            help=(
                "Re-run an existing target instead of submitting new work. "
                "Pass a trial, task, or experiment id (positional, --task, or "
                "--experiment). Retries failed trials by default; combine with "
                "--qa to re-run the task-level QA job (classify every trial + "
                "synthesize the verdict)."
            ),
        ),
    ] = False,
    retry_qa: Annotated[
        bool,
        typer.Option(
            "--qa",
            help=(
                "With --retry: re-run the task-level QA job (classify trials + "
                "verdict) instead of retrying trials."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip confirmation prompts (used with --retry).",
        ),
    ] = False,
    api_url: Annotated[
        str,
        typer.Option(
            "--api",
            help="API URL (defaults to ODDISH_API_URL or Oddish Cloud)",
        ),
    ] = "",
    submit_concurrency: Annotated[
        Optional[int],
        typer.Option(
            "--submit-concurrency",
            help=(
                "Max parallel task uploads/submissions. Default: adaptive "
                f"(grows on success, backs off under load, within "
                f"[{DEFAULT_MIN_CONCURRENCY}, {DEFAULT_MAX_CONCURRENCY}]). "
                f"Overrides ${SUBMIT_CONCURRENCY_ENV_VAR}."
            ),
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Output JSON (for CI/scripts). Implies --background.",
        ),
    ] = False,
):
    """Run Harbor tasks with queues, retries, and monitoring.

    Works like 'harbor run' but gives you automatic retries, provider-aware
    queues, and a status monitor. Uses Harbor's models directly for maximum
    compatibility.

    SINGLE TASK:

        oddish run ./my-task -a claude-code
        oddish run ./my-task -a claude-code -m claude-sonnet-4-5 --n-trials 5

    REGISTRY DATASET:

        # Run on a standard benchmark from the Harbor registry
        oddish run -d swebench@1.0 -a claude-code --n-trials 3
        oddish run -d aider-polyglot -a claude-code

    LOCAL DATASET (directory of tasks):

        # Run on all tasks in a local dataset directory
        oddish run ./my-dataset/ -a claude-code --n-trials 3

    FILTERING (Harbor-compatible):

        # Run specific tasks by name (glob patterns)
        oddish run -d swebench@1.0 -t "django__*" -a claude-code

        # Exclude tasks
        oddish run ./my-dataset -x "*-slow" -a claude-code

        # Limit number of tasks
        oddish run -d swebench@1.0 -l 10 -a claude-code

    COMPLEX SWEEPS (config file):

        For multiple agents/models with different trial counts, use a config:

        oddish run ./my-task -c sweep.yaml

        Example sweep.yaml:

            agents:
              - name: claude-code          # Harbor-style
                model_name: claude-sonnet-4-5
                n_trials: 3
              - name: codex
                model_name: gpt-5.2
                n_trials: 3

            # Optional filtering (same as CLI flags)
            task_names: ["django__*"]
            n_tasks: 10
            harbor:
              environment:
                kwargs:
                  agent_tools_image: ghcr.io/org/harbor-agent-tools:tag

    OTHER OPTIONS:

        oddish run ./task -a claude-code --background   # Submit and return
        oddish run ./task -a claude-code -q             # Quiet mode
        oddish run --task task_123 -a gemini-cli -m google/gemini-3.1-pro-preview
                                                        # Append trials to an existing task

    """
    # Resolve API URL
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)
    is_modal_api = is_modal_api_url(api_url)

    import os as _os

    from oddish.registry_auth import parse_registry_login

    try:
        registry_auth = parse_registry_login(registry_login, dict(_os.environ)) or None
    except ValueError as exc:
        error_console.print(f"[red]Invalid --registry-login:[/red] {exc}")
        raise typer.Exit(1)

    # Retry mode: re-run existing trials, or the task-level QA job, for a
    # target instead of submitting new work. Kept on `run` (rather than a
    # separate command) so the CLI surface stays small.
    if retry:
        from oddish.cli.retry import run_retry

        run_retry(
            api_url,
            target=str(path) if path is not None else None,
            task_id=existing_task_id,
            experiment_id=experiment_id,
            do_qa=retry_qa,
            yes=yes,
            json_output=json_output,
            registry_auth=registry_auth,
        )
        return
    if retry_qa:
        error_console.print("[red]--qa requires --retry.[/red]")
        raise typer.Exit(1)

    # Handle config file vs CLI mode for agent configs
    if config:
        # Config file mode - load agents from file
        sweep_config = load_sweep_config(config)
        configs = sweep_config["agents"]
        harbor_config = copy.deepcopy(sweep_config.get("harbor"))

        # Config can override path, dataset, environment, priority, experiment ID
        if "path" in sweep_config and not path and not path_option and not dataset:
            path_option = Path(sweep_config["path"])
        if "dataset" in sweep_config and not dataset and not path and not path_option:
            dataset = sweep_config["dataset"]
        if "environment" in sweep_config:
            environment = EnvironmentType(sweep_config["environment"])
        if "priority" in sweep_config:
            priority = sweep_config["priority"]
        if "experiment_id" in sweep_config:
            experiment_id = sweep_config["experiment_id"]
        if "max_trial_attempts" in sweep_config and max_trial_attempts is None:
            max_trial_attempts = sweep_config["max_trial_attempts"]
        # Config can also specify filtering (Harbor-compatible)
        if "task_names" in sweep_config and task_names is None:
            task_names = sweep_config["task_names"]
        if "exclude_task_names" in sweep_config and exclude_task_names is None:
            exclude_task_names = sweep_config["exclude_task_names"]
        if "n_tasks" in sweep_config and n_tasks is None:
            n_tasks = sweep_config["n_tasks"]
        # Config can enable analysis
        if "run_analysis" in sweep_config:
            run_analysis = sweep_config["run_analysis"]
        # Config can enable auto-probe
        if "run_probe" in sweep_config:
            run_probe = sweep_config["run_probe"]
        # Config can set Harbor passthrough options
        if "disable_verification" in sweep_config:
            disable_verification = sweep_config["disable_verification"]
        if "override_cpus" in sweep_config and override_cpus is None:
            override_cpus = sweep_config["override_cpus"]
        if "override_memory_mb" in sweep_config and override_memory_mb is None:
            override_memory_mb = sweep_config["override_memory_mb"]
        if "override_gpus" in sweep_config and override_gpus is None:
            override_gpus = sweep_config["override_gpus"]
        if "override_storage_mb" in sweep_config and override_storage_mb is None:
            override_storage_mb = sweep_config["override_storage_mb"]
        if "force_build" in sweep_config and force_build is None:
            force_build = sweep_config["force_build"]

        # Warn if CLI agent/model/n_trials are also specified
        if agent or model or n_trials != 1:
            console.print(
                "[yellow]Warning:[/yellow] --agent, --model, --n-trials are ignored "
                "when using --config"
            )
    else:
        # Simple CLI mode - default agent
        if not agent:
            agent = "claude-code"

        # Build single config
        configs = [
            {
                "agent": agent,
                "model": model,
                "n_trials": n_trials,
            }
        ]
        harbor_config = None

    # Resolve the Harbor source/ref override (CLI --harbor / env ODDISH_HARBOR)
    # with layer-atomic precedence and merge it into the harbor passthrough.
    from oddish.config import resolve_harbor_layers
    from oddish.config import settings as _settings

    _harbor_manifest = _read_harbor_manifest(path, path_option)
    _src, _ref = resolve_harbor_layers(
        flag=harbor, env=_settings.harbor, manifest=_harbor_manifest
    )
    if (harbor and harbor.strip()) or _settings.harbor or _harbor_manifest:
        harbor_config = {**(harbor_config or {}), "source": _src, "ref": _ref}

    # Determine task sources using Harbor's dataset models
    task_paths: list[Path] = []
    existing_task_ids: list[str] = []

    if existing_task_id:
        if dataset or path or path_option:
            error_console.print(
                "[red]Provide either --task, a path, or --dataset, not multiple task sources.[/red]"
            )
            raise typer.Exit(1)
        if task_names or exclude_task_names or n_tasks is not None:
            error_console.print(
                "[red]--task does not support task filtering flags.[/red]"
            )
            raise typer.Exit(1)
        # --experiment is allowed with --task: tasks can belong to multiple
        # experiments (see `task_experiments` M2M in `oddish/db/models.py`),
        # and the server will file the new trials under the provided
        # experiment (auto-linking the task if it isn't already a member).
        existing_task_ids = [existing_task_id]
    else:
        # Shared with ``oddish upload``: resolve path/--path/--dataset
        # into a validated list of local Harbor task directories.
        task_paths = resolve_local_task_paths(
            path=path,
            path_option=path_option,
            dataset=dataset,
            task_names=task_names,
            exclude_task_names=exclude_task_names,
            n_tasks=n_tasks,
            quiet=quiet,
        )

    # Ensure each run uses a single experiment unless specified.
    if not experiment_id and not existing_task_ids:
        experiment_id = generate_experiment_name()

    if environment is None and not existing_task_ids and not is_modal_api:
        environment = EnvironmentType.DOCKER
    elif (
        environment is not None
        and is_modal_api
        and environment not in {EnvironmentType.MODAL, EnvironmentType.DAYTONA}
    ):
        console.print(
            "[yellow]Oddish Cloud supports --env modal and --env daytona; forcing --env modal[/yellow]"
        )
        environment = EnvironmentType.MODAL

    # Upload and submit all tasks
    all_results = []
    total_trials_submitted = 0
    append_mode = bool(existing_task_ids)

    def build_task_payload(
        task_id: str,
        *,
        append_to_task: bool,
        task_content_hash: str | None = None,
        task_path: Path | None = None,
    ) -> dict:
        tags: dict[str, str] = {}
        if github_meta:
            tags["github_meta"] = github_meta

        task_configs = copy.deepcopy(configs)
        task_environment = environment
        if task_environment is None and is_modal_api and task_path is not None:
            task_environment = _default_cloud_environment_for_task(
                task_path,
                override_gpus=override_gpus,
            )
        return build_sweep_payload(
            task_id=task_id,
            configs=task_configs,
            environment=task_environment,
            user=user,
            priority=priority,
            experiment_id=experiment_id,
            max_trial_attempts=max_trial_attempts,
            run_analysis=run_analysis,
            run_probe=run_probe,
            github_username=github_user,
            tags=tags or None,
            publish_experiment=publish,
            disable_verification=disable_verification,
            override_cpus=override_cpus,
            override_memory_mb=override_memory_mb,
            override_gpus=override_gpus,
            override_storage_mb=override_storage_mb,
            force_build=force_build,
            harbor_config=harbor_config,
            environment_kwargs=environment_kwargs,
            agent_env=agent_env,
            agent_kwargs=agent_kwargs,
            artifact_paths=artifact_paths,
            append_to_task=append_to_task,
            content_hash=task_content_hash,
            link=link,
            registry_auth=registry_auth,
        )

    def submit_task(
        task_id: str,
        *,
        append_to_task: bool,
        task_content_hash: str | None = None,
        task_path: Path | None = None,
    ) -> dict:
        payload = build_task_payload(
            task_id,
            append_to_task=append_to_task,
            task_content_hash=task_content_hash,
            task_path=task_path,
        )
        return post_sweep_payload(api_url, payload)

    # Phase 1: upload any local task directories (shared with
    # ``oddish upload``). ``oddish run`` deliberately uses
    # ``register=False`` so the subsequent sweep call owns TaskModel
    # creation: this keeps ``--run-analysis`` working for fresh tasks
    # (the server's append-mode guard rejects enabling run_analysis
    # on an already-registered task that didn't opt in). ``oddish
    # upload`` uses ``register=True`` because its whole purpose is
    # making the task visible before any trials exist.
    #
    # When ``--task`` is used the upload phase is skipped -- we
    # already have a task ID and only need to submit trials against
    # it.
    submit_targets: list[tuple[str, bool, str | None, Path | None]] = []
    if task_paths:
        upload_results = upload_tasks_with_progress(
            api_url,
            task_paths,
            register=False,
            quiet=quiet,
            json_output=json_output,
            progress_label="Uploading",
            force_new_version=force_new_version,
            concurrency=submit_concurrency,
        )
        for task_path, result in zip(task_paths, upload_results):
            is_existing = bool(result.get("existing_task", False))
            if not quiet and is_existing:
                ver = result.get("version", "?")
                if result.get("content_unchanged"):
                    console.print(
                        f"[dim]Task '{task_path.name}' unchanged, reusing version {ver}[/dim]"
                    )
                else:
                    console.print(
                        f"[dim]Task '{task_path.name}' updated, created version {ver}[/dim]"
                    )
            submit_targets.append(
                (
                    result["task_id"],
                    is_existing,
                    result.get("content_hash"),
                    task_path,
                )
            )
    else:
        # --task path: nothing to upload; sweep always appends.
        submit_targets = [(tid, True, None, None) for tid in existing_task_ids]

    # Phase 2: submit trials for every resolved task.
    submit_progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        disable=quiet or json_output,
    )
    with submit_progress:
        progress_task = submit_progress.add_task(
            f"Submitting {len(submit_targets)} task(s)...",
            total=len(submit_targets),
        )
        if len(submit_targets) <= 1:
            for target_id, append, content_hash, task_path in submit_targets:
                result = submit_task(
                    target_id,
                    append_to_task=append,
                    task_content_hash=content_hash,
                    task_path=task_path,
                )
                all_results.append(result)
                total_trials_submitted += result["trials_count"]
                submit_progress.update(progress_task, advance=1)
        else:
            # Prefer the batch endpoint: submit every task in as few requests as
            # possible (chunked under the server's per-request limit). Build all
            # payloads up front, then post them together.
            payloads = [
                build_task_payload(
                    target_id,
                    append_to_task=append,
                    task_content_hash=content_hash,
                    task_path=task_path,
                )
                for target_id, append, content_hash, task_path in submit_targets
            ]
            batch_results = submit_sweep_batch(api_url, payloads)

            if batch_results is not None:
                # Best-effort: each item is independent. Surface failures by
                # task id; keep the tasks that succeeded.
                results_by_index, batch_trials, failed_targets = (
                    _map_batch_sweep_results(batch_results, submit_targets)
                )
                total_trials_submitted += batch_trials
                submit_progress.update(progress_task, advance=len(submit_targets))
                all_results = [r for r in results_by_index if r is not None]
                if failed_targets and not all_results:
                    raise typer.Exit(1)
            else:
                # submit_sweep_batch only returns None for HTTP 404/405 -- an
                # older server without the batch route, where nothing was
                # created -- so it is safe to submit each task on its own.
                # (Ambiguous failures raise instead of returning None.)
                # Throttle this legacy path with the adaptive submit limiter,
                # mirroring the upload pool, so a busy API still shapes
                # submission concurrency.
                submit_limiter = resolve_submit_concurrency(submit_concurrency)

                def _submit_one(
                    target: tuple[str, bool, str | None, Path | None],
                ) -> dict:
                    target_id, append, content_hash, target_path = target
                    return submit_task(
                        target_id,
                        append_to_task=append,
                        task_content_hash=content_hash,
                        task_path=target_path,
                    )

                all_results = map_with_adaptive_concurrency(
                    submit_targets,
                    _submit_one,
                    submit_limiter,
                    on_complete=lambda: submit_progress.update(
                        progress_task, advance=1
                    ),
                )
                total_trials_submitted += sum(r["trials_count"] for r in all_results)

    experiment_id_resolved: str | None = None
    experiment_name = ""

    # Prefer experiment info returned directly by the sweep response (avoids
    # stale task-level experiment_id when appending trials to a new experiment).
    if all_results:
        first = all_results[0]
        experiment_id_resolved = first.get("experiment_id") or None
        experiment_name = first.get("experiment_name") or ""

    # JSON output mode (for CI/scripts)
    if json_output:
        dashboard_url = get_dashboard_url(api_url)
        if all_results and not experiment_id_resolved:
            task_summary = get_task_summary(api_url, all_results[0]["id"])
            if task_summary:
                experiment_id_resolved = task_summary.get("experiment_id")
                experiment_name = task_summary.get("experiment_name") or experiment_name

        experiment_ref = experiment_id_resolved or experiment_name
        experiment_url = (
            f"{dashboard_url}/experiments/{experiment_ref}" if experiment_ref else None
        )
        fallback_url = f"{dashboard_url}/dashboard"
        task_url = experiment_url or fallback_url
        public_experiment_url = None
        if publish and experiment_id_resolved:
            share = get_experiment_share(api_url, experiment_id_resolved)
            token = share.get("public_token") if share else None
            if token:
                public_experiment_url = f"{dashboard_url}/share/{token}"
        output = {
            "experiment": experiment_name,
            "experiment_url": experiment_url,
            "public_experiment_url": public_experiment_url,
            "total_trials": total_trials_submitted,
            "tasks": [
                {
                    "id": r["id"],
                    "trials_count": r["trials_count"],
                    "url": task_url,
                    "public_url": public_experiment_url,
                }
                for r in all_results
            ],
        }
        print(json.dumps(output, indent=2))
        return

    # Print summary (human-readable)
    console.print()
    dashboard_url = get_dashboard_url(api_url)
    if all_results and not experiment_id_resolved:
        task_summary = get_task_summary(api_url, all_results[0]["id"])
        if task_summary:
            experiment_id_resolved = task_summary.get("experiment_id")
            experiment_name = task_summary.get("experiment_name") or experiment_name
    public_experiment_url = None
    if publish and experiment_id_resolved:
        share = get_experiment_share(api_url, experiment_id_resolved)
        token = share.get("public_token") if share else None
        if token:
            public_experiment_url = f"{dashboard_url}/share/{token}"
    if len(all_results) == 1:
        result = all_results[0]
        experiment_ref = experiment_id_resolved or experiment_name
        task_url = (
            f"{dashboard_url}/experiments/{experiment_ref}"
            if experiment_ref
            else f"{dashboard_url}/dashboard"
        )
        console.print(
            "[bold green]Task updated![/bold green]"
            if append_mode
            else "[bold green]Task submitted![/bold green]"
        )
        console.print(f"  Task ID:    {result['id']}")
        console.print(
            f"  {'New trials' if append_mode else 'Trials'}:     {result['trials_count']}"
        )
        console.print(f"  Providers:  {', '.join(result['providers'].keys())}")
        console.print(f"  View:       {task_url}")
        if public_experiment_url:
            console.print(f"  Public:     {public_experiment_url}")
    else:
        summary_verb = "updated" if append_mode else "submitted"
        console.print(
            f"[bold green]{len(all_results)} tasks {summary_verb}![/bold green]"
        )
        console.print(f"  Total trials: {total_trials_submitted}")
        console.print(f"  Experiment:   {experiment_name}")
        experiment_ref = experiment_id_resolved or experiment_name
        if experiment_ref:
            console.print(
                f"  View:         {dashboard_url}/experiments/{experiment_ref}"
            )
        if public_experiment_url:
            console.print(f"  Public:       {public_experiment_url}")

    if not quiet:
        console.print()

    # Background mode: just submit and return
    if background:
        console.print("[dim]Running in background. Check progress with:[/dim]")
        if len(all_results) == 1:
            console.print(f"  oddish status {all_results[0]['id']} --watch")
        return

    # Watch task progress (default behavior) - only for single task
    if watch and len(all_results) == 1:
        if not quiet:
            console.print("[dim]Watching task progress (Ctrl+C to stop)...[/dim]")
            console.print()
        # When appending to an existing task, restrict the live view to the
        # trials we just submitted so prior trials on the same experiment
        # don't clutter the table. For fresh tasks the list is equivalent
        # to the full trial set anyway, so passing it is harmless.
        new_trial_ids = all_results[0].get("new_trial_ids") or None
        try:
            final_result = watch_task(
                api_url,
                all_results[0]["id"],
                experiment_id=experiment_id_resolved,
                trial_ids=new_trial_ids,
            )
            # Print final results table
            if final_result:
                print_final_results(final_result)
        except KeyboardInterrupt:
            console.print(
                "\n[dim]Stopped watching. Task continues in background.[/dim]"
            )
            console.print(
                f"[dim]Resume with: oddish status {all_results[0]['id']} --watch[/dim]"
            )
    elif len(all_results) > 1:
        # Multiple tasks - point to experiment status
        console.print("[dim]Multiple tasks submitted. Monitor with:[/dim]")
        if experiment_id_resolved:
            console.print(
                f"[dim]  oddish status --experiment {experiment_id_resolved} --watch[/dim]"
            )
    else:
        # No watch, no background - just show next steps
        console.print(f"[dim]Next: oddish status {all_results[0]['id']} --watch[/dim]")

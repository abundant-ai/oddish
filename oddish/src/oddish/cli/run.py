from __future__ import annotations

import getpass
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

from oddish.cli.api import (
    get_experiment_share,
    get_task_summary,
    get_task_paths_from_local,
    get_task_paths_from_registry,
    is_task_dir,
    load_sweep_config,
    print_final_results,
    resolve_task_path,
    submit_sweep,
    upload_task,
    watch_task,
)
from oddish.cli.config import (
    error_console,
    get_api_url,
    get_dashboard_url,
    is_local_api_url,
    is_modal_api_url,
    require_api_key,
)
from oddish.cli.infra import check_api_health, ensure_infrastructure
from oddish.experiment import generate_experiment_name

console = Console()


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
    n_trials: Annotated[
        int,
        typer.Option(
            "--n-trials",
            help="Number of trials per task (Oddish-specific; Harbor uses -k for retries)",
        ),
    ] = 1,
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
                "Defaults: docker for local API, daytona for Modal Cloud."
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
            help="User name (defaults to OS username)",
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
    api_url: Annotated[
        str,
        typer.Option(
            "--api",
            help="API URL (default: http://localhost:8000)",
        ),
    ] = "",
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Restart the API server with new settings",
        ),
    ] = False,
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

    OTHER OPTIONS:

        oddish run ./task -a claude-code --background   # Submit and return
        oddish run ./task -a claude-code -q             # Quiet mode

    NOTE: The Oddish API server persists between runs (unlike 'harbor run').
    Use --fresh to restart it, or 'oddish clean' to stop it.
    """
    # Resolve API URL
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)
    is_local_api = is_local_api_url(api_url)
    is_modal_api = is_modal_api_url(api_url)

    # Handle config file vs CLI mode for agent configs
    if config:
        # Config file mode - load agents from file
        sweep_config = load_sweep_config(config)
        configs = sweep_config["agents"]

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
        # Config can set Harbor passthrough options
        if "disable_verification" in sweep_config:
            disable_verification = sweep_config["disable_verification"]
        if "override_cpus" in sweep_config and override_cpus is None:
            override_cpus = sweep_config["override_cpus"]
        if "override_memory_mb" in sweep_config and override_memory_mb is None:
            override_memory_mb = sweep_config["override_memory_mb"]
        if "override_gpus" in sweep_config and override_gpus is None:
            override_gpus = sweep_config["override_gpus"]

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

    # Determine task sources using Harbor's dataset models
    task_paths: list[Path] = []
    dataset_name: str | None = None

    if dataset:
        # Registry dataset mode - use Harbor's RegistryDatasetConfig
        dataset_name = dataset.split("@")[0] if "@" in dataset else dataset
        task_paths = get_task_paths_from_registry(
            dataset_name=dataset,
            task_names=task_names,
            exclude_task_names=exclude_task_names,
            n_tasks=n_tasks,
            quiet=quiet,
        )
    else:
        # Local path mode
        local_path = resolve_task_path(path, path_option)
        if not local_path:
            error_console.print(
                "[red]No task source specified.[/red]\n"
                "Provide a path or use --dataset/-d for registry datasets."
            )
            raise typer.Exit(1)

        if is_task_dir(local_path):
            # Single task
            task_paths = [local_path]
        else:
            # Dataset directory - use Harbor's LocalDatasetConfig
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
            dataset_name = local_path.name
            if not quiet:
                console.print(
                    f"[dim]Found {len(task_paths)} tasks in {local_path}[/dim]"
                )

    # Ensure each run uses a single experiment unless specified.
    if not experiment_id:
        experiment_id = generate_experiment_name()

    # Default user to OS username
    if not user:
        user = getpass.getuser()

    # Ensure infrastructure is running (local only)
    if is_local_api:
        ensure_infrastructure(api_url, quiet=quiet, fresh=fresh)
    else:
        if fresh:
            console.print(
                "[yellow]--fresh only applies to local API; ignoring for remote.[/yellow]"
            )
        # Use longer timeout for remote APIs (Modal cold starts can take a few seconds)
        if not check_api_health(api_url, timeout=10.0):
            error_console.print(
                "[red]API is not reachable or authentication failed.[/red]\n"
                "Verify ODDISH_API_URL and ODDISH_API_KEY."
            )
            raise typer.Exit(1)

    # Default + cloud safety: Modal cannot run Docker-in-Docker.
    if environment is None:
        environment = (
            EnvironmentType.DOCKER
            if is_local_api
            else (EnvironmentType.MODAL if is_modal_api else EnvironmentType.DOCKER)
        )
    elif is_modal_api and environment != EnvironmentType.MODAL:
        console.print(
            "[yellow]Oddish Cloud runs on Modal (no Docker-in-Docker); forcing --env modal[/yellow]"
        )
        environment = EnvironmentType.MODAL

    # Upload and submit all tasks
    all_results = []
    total_trials_submitted = 0

    show_progress = not quiet and not json_output
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        disable=not show_progress,
    )
    with progress:
        upload_task_progress = progress.add_task(
            f"Uploading {len(task_paths)} tasks...", total=len(task_paths)
        )

        for task_path in task_paths:
            # Upload task
            task_id = upload_task(api_url, task_path)

            tags: dict[str, str] = {}
            if github_meta:
                tags["github_meta"] = github_meta

            # Submit sweep for this task
            result = submit_sweep(
                api_url=api_url,
                task_id=task_id,
                configs=configs,
                environment=environment,
                user=user,
                priority=priority,
                experiment_id=experiment_id,
                run_analysis=run_analysis,
                github_username=github_user,
                tags=tags or None,
                publish_experiment=publish,
                disable_verification=disable_verification,
                override_cpus=override_cpus,
                override_memory_mb=override_memory_mb,
                override_gpus=override_gpus,
                override_storage_mb=override_storage_mb,
                force_build=force_build,
                agent_env=agent_env,
                agent_kwargs=agent_kwargs,
                artifact_paths=artifact_paths,
            )
            all_results.append(result)
            total_trials_submitted += result["trials_count"]
            progress.update(upload_task_progress, advance=1)

    experiment_id_resolved: str | None = None
    experiment_name = ""

    # JSON output mode (for CI/scripts)
    if json_output:
        dashboard_url = get_dashboard_url(api_url)
        if all_results:
            task_summary = get_task_summary(api_url, all_results[0]["id"])
            if task_summary:
                experiment_id_resolved = task_summary.get("experiment_id")
                experiment_name = task_summary.get("experiment_name") or experiment_name

        experiment_ref = experiment_id_resolved or experiment_name
        experiment_url = (
            f"{dashboard_url}/experiments/{experiment_ref}" if experiment_ref else None
        )
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
                    "url": f"{dashboard_url}/tasks/{r['id']}",
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
    if all_results:
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
        console.print("[bold green]Task submitted![/bold green]")
        console.print(f"  Task ID:    {result['id']}")
        console.print(f"  Trials:     {result['trials_count']}")
        console.print(f"  Providers:  {', '.join(result['providers'].keys())}")
        console.print(f"  View:       {dashboard_url}/tasks/{result['id']}")
        if public_experiment_url:
            console.print(f"  Public:     {public_experiment_url}")
    else:
        console.print(f"[bold green]{len(all_results)} tasks submitted![/bold green]")
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
        try:
            final_result = watch_task(api_url, all_results[0]["id"])
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

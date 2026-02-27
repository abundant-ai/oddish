from __future__ import annotations

import json
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import cast

import httpx
import typer
import yaml
from rich.console import Console
from rich.live import Live
from rich.table import Table

from harbor.models.environment_type import EnvironmentType
from harbor.models.task.paths import TaskPaths
from harbor.models.registry import RemoteRegistryInfo
from harbor.models.trial.config import AgentConfig
from harbor.models.job.config import LocalDatasetConfig, RegistryDatasetConfig
from harbor.dataset.client import DatasetClient

from oddish.cli.config import get_auth_headers, error_console

console = Console()


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


def get_task_paths_from_local(
    dataset_path: Path,
    task_names: list[str] | None = None,
    exclude_task_names: list[str] | None = None,
    n_tasks: int | None = None,
) -> list[Path]:
    """Get task paths from a local dataset directory using Harbor's LocalDatasetConfig."""
    config = LocalDatasetConfig(
        path=dataset_path,
        task_names=task_names,
        exclude_task_names=exclude_task_names,
        n_tasks=n_tasks,
    )
    task_configs = config.get_task_configs()
    return [tc.path for tc in task_configs]


def get_task_paths_from_registry(
    dataset_name: str,
    version: str | None = None,
    task_names: list[str] | None = None,
    exclude_task_names: list[str] | None = None,
    n_tasks: int | None = None,
    quiet: bool = False,
) -> list[Path]:
    """Get task paths from Harbor registry using Harbor's RegistryDatasetConfig."""
    # Parse name@version format
    if "@" in dataset_name and version is None:
        dataset_name, version = dataset_name.split("@", 1)

    if not quiet:
        console.print(
            f"[dim]Fetching dataset from registry: {dataset_name}@{version or 'latest'}[/dim]"
        )

    try:
        config = RegistryDatasetConfig(
            registry=RemoteRegistryInfo(),
            name=dataset_name,
            version=version,
            task_names=task_names,
            exclude_task_names=exclude_task_names,
            n_tasks=n_tasks,
        )

        # Use DatasetClient to download and get actual local paths
        client = DatasetClient()
        downloaded_tasks = client.download_dataset_from_config(config)

        if not quiet:
            console.print(f"[green]Downloaded {len(downloaded_tasks)} tasks[/green]")

        return [task.local_path for task in downloaded_tasks]

    except Exception as e:
        error_console.print(f"[red]Failed to download dataset:[/red] {e}")
        raise typer.Exit(1)


# =============================================================================
# Task Upload & Submit
# =============================================================================


def archive_task_dir(task_path: Path) -> Path:
    """Create a tarball of a task directory."""
    # Create tarball in temp directory
    tmpdir = tempfile.mkdtemp()
    tarball_path = Path(tmpdir) / f"{task_path.name}.tar.gz"

    with tarfile.open(tarball_path, "w:gz") as tar:
        # Add contents of task_path to the tarball
        for item in task_path.iterdir():
            tar.add(item, arcname=item.name)

    return tarball_path


def upload_task(
    api_url: str,
    task_path: Path,
) -> str:
    """Upload a task directory to the API. Returns task_id."""
    tarball_path = archive_task_dir(task_path)

    try:
        with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
            with open(tarball_path, "rb") as f:
                response = client.post(
                    f"{api_url}/tasks/upload",
                    files={
                        "file": (
                            f"{task_path.name}.tar.gz",
                            f,
                            "application/gzip",
                        )
                    },
                )

        if response.status_code != 200:
            error_console.print(f"[red]Failed to upload task:[/red] {response.text}")
            raise typer.Exit(1)

        result: str = response.json()["task_id"]
        return result
    finally:
        # Cleanup tarball
        shutil.rmtree(Path(tarball_path).parent, ignore_errors=True)


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
    environment: EnvironmentType,
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
) -> dict:
    """Submit a task sweep to the API."""
    env_value = environment.value

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
        "experiment_id": experiment_id,
        "environment": env_value,
        "run_analysis": run_analysis,
    }

    if github_username:
        payload["github_username"] = github_username
    if tags:
        payload["tags"] = tags
    payload["publish_experiment"] = publish_experiment
    if harbor:
        payload["harbor"] = harbor

    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
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
    normalized_agents = []
    for i, agent_entry in enumerate(config["agents"]):
        agent_data = {
            "name": agent_entry.get("name"),
            "model_name": agent_entry.get("model_name"),
        }

        if not agent_data["name"]:
            error_console.print(f"[red]Agent entry {i+1} missing 'name' field[/red]")
            raise typer.Exit(1)
        if agent_data["model_name"] is None:
            error_console.print(
                f"[red]Agent entry {i+1} missing 'model_name' field[/red]"
            )
            raise typer.Exit(1)

        # Validate using Harbor's AgentConfig model (validates name, model_name, etc.)
        try:
            harbor_config = AgentConfig.model_validate(agent_data)
        except Exception as e:
            error_console.print(f"[red]Invalid agent config at entry {i+1}:[/red] {e}")
            raise typer.Exit(1)

        if "n_concurrent" in agent_entry or "concurrency" in agent_entry:
            error_console.print(
                f"[red]Agent entry {i+1} includes 'n_concurrent', which is no longer supported.[/red]\n"
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
            entry["timeout_minutes"] = agent_entry["timeout_minutes"]
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


def get_task_result(api_url: str, task_id: str) -> dict | None:
    """Fetch the final task result from the API."""
    try:
        with httpx.Client(timeout=10.0, headers=get_auth_headers()) as client:
            response = client.get(f"{api_url}/tasks/{task_id}")
        if response.status_code == 200:
            return cast(dict, response.json())
    except Exception:
        pass
    return None


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
            rewards.append(reward)
            reward_str = f"{reward:.2f}" if isinstance(reward, float) else str(reward)
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
        summary_parts.append(f"avg reward: [cyan]{avg_reward:.2f}[/cyan]")

    console.print("  " + " | ".join(summary_parts))
    console.print()


def watch_task(api_url: str, task_id: str) -> dict | None:
    """Watch a task until completion. Returns the final result."""
    final_result = None
    headers = get_auth_headers()
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

                task_status = result.get("status", "unknown")
                task_status_display = format_task_status(task_status)

                # Build status table
                table = Table(title=f"Task: {task_id}  {task_status_display}")
                table.add_column("#", style="cyan", justify="right")
                table.add_column("Agent")
                table.add_column("Model")
                table.add_column("Status")
                table.add_column("Reward", justify="center")

                for trial in result.get("trials", []):
                    status = trial["status"]
                    harbor_stage = trial.get("harbor_stage")
                    status_display = format_trial_status(status, harbor_stage)

                    reward = trial.get("reward")
                    if reward == 1:
                        reward_str = "[green]✓[/green]"
                    elif reward == 0:
                        reward_str = "[red]✗[/red]"
                    else:
                        reward_str = "-"

                    table.add_row(
                        trial["id"].split("-")[-1],  # Just the index
                        trial["agent"],
                        trial.get("model") or "-",
                        status_display,
                        reward_str,
                    )

                # Add summary row
                total = result.get("total", 0)
                completed = result.get("completed", 0)
                failed = result.get("failed", 0)

                # Count rewards
                trials = result.get("trials", [])
                reward_pass = sum(1 for t in trials if t.get("reward") == 1)
                reward_fail = sum(1 for t in trials if t.get("reward") == 0)

                table.add_section()
                summary_parts = [f"[bold]{completed}/{total}[/bold] done"]
                if failed > 0:
                    summary_parts.append(f"[red]{failed} failed[/red]")
                if reward_pass > 0 or reward_fail > 0:
                    summary_parts.append(
                        f"[green]{reward_pass}✓[/green]/[red]{reward_fail}✗[/red]"
                    )

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
                if task_status in ("completed", "failed"):
                    break

                time.sleep(2)

            except Exception as e:
                live.update(f"[red]Error:[/red] {e}")
                time.sleep(2)

    return final_result

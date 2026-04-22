"""`oddish upload` -- upload Harbor tasks or off-oddish trial results.

This single command handles two related flows:

1. **Task upload** -- register Harbor task bundles on the server so
   they appear in the task browser, ready for later sweeps, appends,
   or imports. No trials are queued.

2. **Trial import** -- register trials that were executed outside of
   Oddish (e.g. a local ``harbor run``) as regular trial rows on an
   existing task. Imported trials show up alongside live ones with
   their reward, tokens, cost, phase timing, and artifacts; only the
   ``origin = "imported"`` flag on the row distinguishes them.

The flow is picked automatically from the positional path:

- ``is_task_dir(path)``             -> single task upload
- a dataset dir of task subdirs      -> multi-task upload
- ``--dataset`` flag                 -> registry dataset upload
- ``is_harbor_job_dir(path)``        -> trial import (one Harbor job)
- ``is_harbor_jobs_dir(path)``       -> trial import (many Harbor jobs)

Trial-import mode requires an explicit target task. Pass ``--task
<id>`` to attach to an existing task, or ``--path <task_dir>`` to
upload a task first and import against it in one command.
"""

from __future__ import annotations

import getpass
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from oddish.cli.api import (
    discover_trial_entries,
    import_trial,
    is_harbor_job_dir,
    is_harbor_jobs_dir,
    resolve_local_task_paths,
    upload_task,
    upload_tasks_with_progress,
)
from oddish.cli.config import (
    error_console,
    get_api_url,
    get_dashboard_url,
    require_api_key,
)

console = Console()

# Trial imports are small (per-trial tarballs) and independent, so we
# allow a modest fan-out. Task uploads share
# ``cli.api.TASK_UPLOAD_CONCURRENCY`` via ``upload_tasks_with_progress``.
TRIAL_IMPORT_CONCURRENCY = 4


# =============================================================================
# Command
# =============================================================================


def upload(
    path: Annotated[
        Optional[Path],
        typer.Argument(
            help=(
                "Path to a task, dataset, Harbor job dir, or Harbor jobs "
                "parent dir. The flow is auto-detected from the contents."
            ),
        ),
    ] = None,
    path_option: Annotated[
        Optional[Path],
        typer.Option(
            "--path",
            "-p",
            help=(
                "Path to task/dataset dir (Harbor-compatible alias). In "
                "trial-import mode, uploads this task first and then "
                "imports the trials against it."
            ),
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
            help="Maximum number of tasks to upload (applied after filters)",
        ),
    ] = None,
    user: Annotated[
        Optional[str],
        typer.Option(
            "--user",
            "-u",
            help="User name to attribute the uploaded task to (defaults to OS username)",
        ),
    ] = None,
    priority: Annotated[
        str,
        typer.Option(
            "--priority",
            "-P",
            help="Priority (low or high) stamped on the created task row",
        ),
    ] = "low",
    message: Annotated[
        Optional[str],
        typer.Option(
            "--message",
            "-M",
            help="Optional description attached to this task version",
        ),
    ] = None,
    task_id: Annotated[
        Optional[str],
        typer.Option(
            "--task",
            help=(
                "Target task ID for trial-import mode. Upload the task "
                "first with `oddish upload ./task` or pass --path to do "
                "both in one step."
            ),
        ),
    ] = None,
    experiment_id: Annotated[
        Optional[str],
        typer.Option(
            "--experiment",
            "-E",
            help=(
                "Experiment ID or name for imported trials. Creates the "
                "experiment if the name doesn't exist. Default: "
                "auto-generate a fresh experiment (matches `oddish run`)."
            ),
        ),
    ] = None,
    skip_artifacts: Annotated[
        bool,
        typer.Option(
            "--skip-artifacts",
            help=(
                "Trial-import only: register trial metadata (reward, "
                "tokens, timing) without uploading logs/trajectory."
            ),
        ),
    ] = False,
    api_url: Annotated[
        str,
        typer.Option(
            "--api",
            help="API URL (defaults to ODDISH_API_URL or Oddish Cloud)",
        ),
    ] = "",
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Suppress nonessential output",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Output JSON (for CI/scripts)",
        ),
    ] = False,
):
    """Upload Harbor tasks or off-oddish trial results to Oddish.

    TASK UPLOAD (no trials queued):

        oddish upload ./my-task                       # single task
        oddish upload ./my-dataset                    # local dataset dir
        oddish upload -d swebench@1.0                 # registry dataset
        oddish upload ./my-dataset -t "django__*"     # filter tasks

    TRIAL IMPORT (off-oddish harbor run):

        # Import every trial in a single Harbor job dir
        oddish upload ./jobs/my-task.claude-code.abcd --task task_123

        # Pin imports to an experiment (new or existing)
        oddish upload ./jobs/my-task.claude-code.abcd --task task_123 \\
            --experiment my-local-sweep

        # Import many jobs at once from a parent jobs dir
        oddish upload ./jobs --task task_123 -E my-sweep

        # Upload the task and import its trials in one command
        oddish upload ./jobs/my-task.claude-code.abcd --path ./my-task

        # Register metadata only (no logs/trajectory uploads)
        oddish upload ./jobs/my-task.claude-code.abcd --task task_123 \\
            --skip-artifacts

    The positional PATH is inspected to decide which flow to use.
    """
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    # ------------------------------------------------------------------
    # Detect whether the caller is uploading tasks or importing trials.
    # Order matters: a Harbor job dir has ``result.json`` at the top but
    # is never also a task dir, so we check harbor-shaped first.
    # ------------------------------------------------------------------
    harbor_job_path: Path | None = None
    if path is not None and (is_harbor_job_dir(path) or is_harbor_jobs_dir(path)):
        harbor_job_path = path
    elif path is None and path_option is not None and (
        is_harbor_job_dir(path_option) or is_harbor_jobs_dir(path_option)
    ):
        # When the user passes the harbor dir via --path (no positional),
        # still route into trial-import mode. The task-source --path
        # convenience flag is only honored when the positional points at
        # a harbor dir *and* --path at a task dir.
        harbor_job_path = path_option
        path_option = None

    if harbor_job_path is not None:
        _run_trial_import(
            api_url=api_url,
            harbor_job_path=harbor_job_path,
            task_id_opt=task_id,
            path_option=path_option,
            experiment_id=experiment_id,
            user=user,
            skip_artifacts=skip_artifacts,
            quiet=quiet,
            json_output=json_output,
        )
        return

    # ------------------------------------------------------------------
    # Task upload mode. Most flags from import mode are disallowed so we
    # don't silently ignore them.
    # ------------------------------------------------------------------
    if task_id is not None:
        error_console.print(
            "[red]--task is only valid in trial-import mode. "
            "Point the positional PATH at a Harbor job directory.[/red]"
        )
        raise typer.Exit(1)
    if experiment_id is not None:
        error_console.print(
            "[red]--experiment is only valid in trial-import mode. "
            "Attach tasks to experiments via `oddish run` once they "
            "have trials.[/red]"
        )
        raise typer.Exit(1)
    if skip_artifacts:
        error_console.print(
            "[red]--skip-artifacts is only valid in trial-import mode.[/red]"
        )
        raise typer.Exit(1)

    _run_task_upload(
        api_url=api_url,
        path=path,
        path_option=path_option,
        dataset=dataset,
        task_names=task_names,
        exclude_task_names=exclude_task_names,
        n_tasks=n_tasks,
        user=user,
        priority=priority,
        message=message,
        quiet=quiet,
        json_output=json_output,
    )


# =============================================================================
# Task upload flow
# =============================================================================


def _run_task_upload(
    *,
    api_url: str,
    path: Path | None,
    path_option: Path | None,
    dataset: str | None,
    task_names: list[str] | None,
    exclude_task_names: list[str] | None,
    n_tasks: int | None,
    user: str | None,
    priority: str,
    message: str | None,
    quiet: bool,
    json_output: bool,
) -> None:
    # Step 1 (shared with ``oddish run``): resolve path/--path/--dataset
    # into a validated list of Harbor task directories.
    task_paths = resolve_local_task_paths(
        path=path,
        path_option=path_option,
        dataset=dataset,
        task_names=task_names,
        exclude_task_names=exclude_task_names,
        n_tasks=n_tasks,
        quiet=quiet,
    )

    if not user:
        user = getpass.getuser()

    # Step 2 (shared with ``oddish run``): upload each archive and
    # register the TaskModel. ``oddish upload`` always uses
    # ``register=True`` so the task becomes browsable even without
    # trials; ``oddish run`` passes the same flag so tasks show up
    # immediately and the subsequent sweep call auto-appends.
    results = upload_tasks_with_progress(
        api_url,
        task_paths,
        register=True,
        message=message,
        user=user,
        priority=priority,
        quiet=quiet,
        json_output=json_output,
    )

    dashboard_url = get_dashboard_url(api_url)

    if json_output:
        # There's no per-task frontend page today (only the /tasks
        # browser and experiment pages), so we emit the browser URL
        # instead of fabricating a 404-prone /tasks/<id> link.
        output = {
            "mode": "task_upload",
            "tasks": [
                {
                    "id": r.get("task_id"),
                    "name": r.get("name"),
                    "version": r.get("version"),
                    "existing_task": r.get("existing_task", False),
                    "content_unchanged": r.get("content_unchanged", False),
                    "url": f"{dashboard_url}/tasks",
                }
                for r in results
            ],
        }
        print(json.dumps(output, indent=2))
        return

    console.print()
    if len(results) == 1:
        r = results[0]
        uploaded_task_id = r.get("task_id")
        version = r.get("version", "?")
        name = r.get("name") or ""
        if r.get("content_unchanged"):
            console.print(
                f"[bold green]Task unchanged[/bold green] — reused version {version}"
            )
        elif r.get("existing_task"):
            console.print(
                f"[bold green]Task updated[/bold green] — created version {version}"
            )
        else:
            console.print(
                f"[bold green]Task uploaded[/bold green] — version {version}"
            )
        console.print(f"  Task ID:  {uploaded_task_id}")
        if name:
            console.print(f"  Name:     {name}")
        # Tasks are browsed at /tasks (no dedicated detail page today);
        # search by name to find the row.
        console.print(f"  Browse:   {dashboard_url}/tasks")
        console.print()
        console.print(
            f"[dim]Next: oddish run --task {uploaded_task_id} -a <agent> -m <model>[/dim]"
        )
    else:
        new_count = sum(1 for r in results if not r.get("existing_task"))
        updated_count = sum(
            1
            for r in results
            if r.get("existing_task") and not r.get("content_unchanged")
        )
        unchanged_count = sum(1 for r in results if r.get("content_unchanged"))
        console.print(f"[bold green]{len(results)} tasks uploaded![/bold green]")
        console.print(f"  New:       {new_count}")
        console.print(f"  Updated:   {updated_count}")
        console.print(f"  Unchanged: {unchanged_count}")
        console.print(f"  Dashboard: {dashboard_url}/tasks")


# =============================================================================
# Trial import flow
# =============================================================================


def _run_trial_import(
    *,
    api_url: str,
    harbor_job_path: Path,
    task_id_opt: str | None,
    path_option: Path | None,
    experiment_id: str | None,
    user: str | None,
    skip_artifacts: bool,
    quiet: bool,
    json_output: bool,
) -> None:
    if task_id_opt and path_option:
        error_console.print(
            "[red]Provide either --task or --path, not both.[/red]"
        )
        raise typer.Exit(1)

    resolved_task_id = task_id_opt

    # One-shot: upload the task alongside the trial import.
    if path_option is not None:
        if not user:
            user = getpass.getuser()
        if not quiet:
            console.print(f"[dim]Uploading task from {path_option}...[/dim]")
        upload_result = upload_task(
            api_url,
            path_option,
            register=True,
            user=user,
        )
        resolved_task_id = upload_result.get("task_id")
        if not resolved_task_id:
            error_console.print(
                "[red]Task upload did not return a task_id; cannot import trials.[/red]"
            )
            raise typer.Exit(1)
        if not quiet:
            console.print(f"[dim]Task uploaded: {resolved_task_id}[/dim]")

    if not resolved_task_id:
        error_console.print(
            "[red]--task (or --path) is required to identify which task "
            "to attach imported trials to.[/red]\n"
            "Upload the task first with `oddish upload ./task`, or pass "
            "it inline via --path."
        )
        raise typer.Exit(1)

    entries = discover_trial_entries(harbor_job_path)
    if not entries:
        error_console.print(
            f"[red]No Harbor trials found under {harbor_job_path}.[/red]\n"
            "Expected a harbor job dir with per-trial subdirs (each "
            "containing a result.json)."
        )
        raise typer.Exit(1)

    # Pin ALL trials in this invocation to a single experiment. Without
    # this, every concurrent import call generates its own auto-named
    # experiment server-side, so a user running `oddish upload ./jobs
    # --task X` with 10 trials would end up with 10 separate 1-trial
    # experiments instead of one 10-trial experiment.
    #
    # Generating the name client-side lets the first server call create
    # the experiment row and every subsequent call reuse it via
    # ``get_experiment_by_id_or_name``. Same behavior as ``oddish run``
    # which auto-generates an experiment name in the CLI before calling
    # the server.
    effective_experiment_id = experiment_id
    if not effective_experiment_id:
        from oddish.experiment import generate_experiment_name

        effective_experiment_id = generate_experiment_name()
        if not quiet:
            console.print(
                f"[dim]Creating experiment: {effective_experiment_id}[/dim]"
            )

    if not quiet:
        console.print(
            f"[dim]Found {len(entries)} trial(s) to import from {harbor_job_path}[/dim]"
        )

    upload_artifacts = not skip_artifacts

    def _import_one(entry: tuple[str, str, Path]) -> dict[str, Any]:
        job_name, trial_name, trial_dir = entry
        try:
            init = import_trial(
                api_url,
                task_id=resolved_task_id,
                experiment_id=effective_experiment_id,
                trial_dir=trial_dir,
                upload_artifacts=upload_artifacts,
            )
            return {
                "job_name": job_name,
                "trial_name": trial_name,
                "trial_id": init.get("trial_id"),
                "experiment_id": init.get("experiment_id"),
                "experiment_name": init.get("experiment_name"),
                "files_extracted": init.get("files_extracted", 0),
                "status": "imported",
            }
        except typer.Exit:
            # The underlying helpers already printed a red error;
            # record the failure so other trials keep going.
            return {
                "job_name": job_name,
                "trial_name": trial_name,
                "status": "error",
                "error": "import failed; see error above",
            }

    show_progress = not quiet and not json_output
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        disable=not show_progress,
    )

    results: list[dict[str, Any]] = []
    with progress:
        import_progress = progress.add_task(
            f"Importing {len(entries)} trials...", total=len(entries)
        )
        if len(entries) <= 1:
            for entry in entries:
                results.append(_import_one(entry))
                progress.update(import_progress, advance=1)
        else:
            # Run the first import sequentially so the shared experiment
            # row is created atomically before we fan out. Without this,
            # the 4-way thread pool would race inside
            # ``get_or_create_experiment`` (which is a lookup-then-insert
            # with no unique constraint to serialize), and we'd end up
            # splitting the trials across multiple experiments with the
            # same name.
            first_result = _import_one(entries[0])
            results.append(first_result)
            progress.update(import_progress, advance=1)

            remaining = entries[1:]
            if remaining:
                results_by_index: list[dict[str, Any] | None] = [None] * len(remaining)
                max_workers = min(TRIAL_IMPORT_CONCURRENCY, len(remaining))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_index = {
                        executor.submit(_import_one, entry): index
                        for index, entry in enumerate(remaining)
                    }
                    for future in as_completed(future_to_index):
                        index = future_to_index[future]
                        results_by_index[index] = future.result()
                        progress.update(import_progress, advance=1)
                results.extend(r for r in results_by_index if r is not None)

    dashboard_url = get_dashboard_url(api_url)
    experiment_ids = {
        r.get("experiment_id") for r in results if r.get("experiment_id")
    }
    experiment_ref = (
        next(iter(experiment_ids)) if len(experiment_ids) == 1 else None
    )

    if json_output:
        output = {
            "mode": "trial_import",
            "task_id": resolved_task_id,
            "experiment_id": experiment_ref,
            "experiment_url": (
                f"{dashboard_url}/experiments/{experiment_ref}"
                if experiment_ref
                else None
            ),
            "trials": results,
        }
        print(json.dumps(output, indent=2, default=str))
        if any(r.get("status") == "error" for r in results):
            raise typer.Exit(code=2)
        return

    imported = sum(1 for r in results if r.get("status") == "imported")
    errored = sum(1 for r in results if r.get("status") == "error")

    console.print()
    if imported:
        console.print(
            f"[bold green]Imported {imported} trial(s)[/bold green] into task "
            f"{resolved_task_id}"
        )
    if errored:
        console.print(f"[yellow]{errored} trial(s) failed to import[/yellow]")

    if experiment_ref:
        console.print(
            f"  Experiment: {dashboard_url}/experiments/{experiment_ref}"
        )
    else:
        # No unified experiment to link to -- point at the task browser
        # since there's no /tasks/<id> frontend page today.
        console.print(f"  Dashboard:  {dashboard_url}/tasks")

    if errored and not imported:
        raise typer.Exit(code=2)

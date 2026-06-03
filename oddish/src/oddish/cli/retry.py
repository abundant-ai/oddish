from __future__ import annotations

from typing import Literal, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.api import get_experiment_tasks, get_task_summary
from oddish.cli.config import get_auth_headers, print_json

console = Console()
error_console = Console(stderr=True)

TargetType = Literal["trial", "task", "experiment"]

# Trial statuses that are eligible for a re-run when retrying in bulk
# (task / experiment targets). The backend additionally accepts SUCCESS and
# "stuck" trials for an explicit single-trial retry, but bulk retries only
# sweep up the trials a user almost always means: the failed ones.
_RETRYABLE_BULK_STATUSES = {"failed"}


def _split_trial_id(value: str) -> str | None:
    """Return the parent task id for a ``<task_id>-<index>`` trial id."""
    task_id, sep, maybe_index = value.rpartition("-")
    if not sep or not maybe_index.isdigit():
        return None
    return task_id or None


def _resolve_target(
    api_url: str,
    *,
    target: str | None,
    task_id: str | None,
    experiment_id: str | None,
) -> tuple[TargetType, str]:
    """Resolve the retry target to a (type, id) pair.

    Explicit ``--task`` / ``--experiment`` selectors win; otherwise the
    positional argument is auto-resolved as a trial, task, or experiment id
    (in that order) the same way ``oddish pull`` resolves targets.
    """
    selectors = [bool(experiment_id), bool(task_id), bool(target)]
    if sum(selectors) == 0:
        raise typer.BadParameter(
            "Provide a trial, task, or experiment id to retry "
            "(positional argument, --task, or --experiment)."
        )
    if sum(selectors) > 1:
        raise typer.BadParameter(
            "Provide exactly one retry target: a positional id, --task, "
            "or --experiment."
        )

    if experiment_id:
        return "experiment", experiment_id
    if task_id:
        return "task", task_id

    assert target is not None
    parent_task_id = _split_trial_id(target)
    if parent_task_id:
        task = get_task_summary(api_url, parent_task_id)
        if task and any(t.get("id") == target for t in task.get("trials", []) or []):
            return "trial", target

    if get_task_summary(api_url, target) is not None:
        return "task", target

    if get_experiment_tasks(api_url, target):
        return "experiment", target

    raise typer.BadParameter(
        f"Unable to resolve '{target}' as a trial, task, or experiment id."
    )


def _post(api_url: str, path: str) -> httpx.Response:
    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        return client.post(f"{api_url}{path}")


def _failed_trial_ids(task: dict) -> list[str]:
    """Live, failed trial ids for a task summary payload."""
    ids: list[str] = []
    for trial in task.get("trials", []) or []:
        if trial.get("superseded_by_trial_id"):
            continue
        if str(trial.get("status", "")).lower() in _RETRYABLE_BULK_STATUSES:
            tid = trial.get("id")
            if tid:
                ids.append(tid)
    return ids


def _retry_trial_ids(api_url: str, trial_ids: list[str]) -> list[dict]:
    """Retry each trial id, returning a per-trial result record."""
    results: list[dict] = []
    for trial_id in trial_ids:
        response = _post(api_url, f"/trials/{trial_id}/retry")
        ok = response.status_code == 200
        record: dict = {"trial_id": trial_id, "ok": ok, "status": response.status_code}
        if ok:
            try:
                record["response"] = response.json()
            except ValueError:
                record["response"] = None
        else:
            record["error"] = response.text
        results.append(record)
    return results


def _task_action(api_url: str, task_id: str, kind: str) -> dict:
    """Run a task-level analysis or verdict retry."""
    path = f"/tasks/{task_id}/{'analysis' if kind == 'analysis' else 'verdict'}/retry"
    response = _post(api_url, path)
    ok = response.status_code == 200
    record: dict = {"task_id": task_id, "ok": ok, "status": response.status_code}
    if ok:
        try:
            record["response"] = response.json()
        except ValueError:
            record["response"] = None
    else:
        record["error"] = response.text
    return record


def run_retry(
    api_url: str,
    *,
    target: str | None,
    task_id: Optional[str],
    experiment_id: Optional[str],
    do_analysis: bool,
    do_verdict: bool,
    yes: bool,
    json_output: bool,
) -> None:
    """Re-run trials, analysis, or verdict for an existing target.

    Backs ``oddish run <id> --retry`` (and ``--analysis`` / ``--verdict``).
    """
    if do_analysis and do_verdict:
        message = "Use only one of --analysis or --verdict with --retry."
        if json_output:
            print_json({"error": message})
        else:
            error_console.print(f"[red]{message}[/red]")
        raise typer.Exit(1)

    kind = "analysis" if do_analysis else "verdict" if do_verdict else "trials"

    target_type, target_id = _resolve_target(
        api_url,
        target=target,
        task_id=task_id,
        experiment_id=experiment_id,
    )

    # Build the worklist of (action, id) operations up front so we can show a
    # confirmation and emit a single structured summary.
    if kind == "trials":
        results = _run_trial_retries(
            api_url,
            target_type,
            target_id,
            yes=yes,
            json_output=json_output,
        )
    else:
        results = _run_task_level_retries(
            api_url,
            target_type,
            target_id,
            kind=kind,
            yes=yes,
            json_output=json_output,
        )

    _report(results, json_output=json_output)


def _confirm(prompt: str, *, yes: bool, json_output: bool) -> None:
    if yes or json_output:
        return
    if not typer.confirm(prompt, default=True):
        raise typer.Abort()


def _run_trial_retries(
    api_url: str,
    target_type: TargetType,
    target_id: str,
    *,
    yes: bool,
    json_output: bool,
) -> dict:
    if target_type == "trial":
        _confirm(f"Retry trial {target_id}?", yes=yes, json_output=json_output)
        trial_results = _retry_trial_ids(api_url, [target_id])
        return {
            "kind": "trials",
            "target": {"type": "trial", "id": target_id},
            "trials": trial_results,
        }

    if target_type == "task":
        task = get_task_summary(api_url, target_id)
        if task is None:
            raise typer.BadParameter(f"Task '{target_id}' not found.")
        trial_ids = _failed_trial_ids(task)
        if not trial_ids:
            return {
                "kind": "trials",
                "target": {"type": "task", "id": target_id},
                "trials": [],
                "note": "No failed trials to retry.",
            }
        _confirm(
            f"Retry {len(trial_ids)} failed trial(s) in task {target_id}?",
            yes=yes,
            json_output=json_output,
        )
        return {
            "kind": "trials",
            "target": {"type": "task", "id": target_id},
            "trials": _retry_trial_ids(api_url, trial_ids),
        }

    # experiment
    tasks = get_experiment_tasks(api_url, target_id) or []
    trial_ids: list[str] = []
    for task in tasks:
        trial_ids.extend(_failed_trial_ids(task))
    if not trial_ids:
        return {
            "kind": "trials",
            "target": {"type": "experiment", "id": target_id},
            "trials": [],
            "note": "No failed trials to retry.",
        }
    _confirm(
        f"Retry {len(trial_ids)} failed trial(s) in experiment {target_id}?",
        yes=yes,
        json_output=json_output,
    )
    return {
        "kind": "trials",
        "target": {"type": "experiment", "id": target_id},
        "trials": _retry_trial_ids(api_url, trial_ids),
    }


def _run_task_level_retries(
    api_url: str,
    target_type: TargetType,
    target_id: str,
    *,
    kind: str,
    yes: bool,
    json_output: bool,
) -> dict:
    # Analysis/verdict are task-scoped; map a trial target to its parent task,
    # except trial-level analysis which has its own endpoint.
    if target_type == "trial":
        if kind == "analysis":
            _confirm(
                f"Re-run analysis for trial {target_id}?",
                yes=yes,
                json_output=json_output,
            )
            response = _post(api_url, f"/trials/{target_id}/analysis/retry")
            ok = response.status_code == 200
            record: dict = {
                "trial_id": target_id,
                "ok": ok,
                "status": response.status_code,
            }
            if ok:
                try:
                    record["response"] = response.json()
                except ValueError:
                    record["response"] = None
            else:
                record["error"] = response.text
            return {
                "kind": "analysis",
                "target": {"type": "trial", "id": target_id},
                "tasks": [record],
            }
        # verdict is not a per-trial concept: resolve the parent task.
        parent = _split_trial_id(target_id)
        if not parent:
            raise typer.BadParameter(
                f"Cannot resolve parent task for trial '{target_id}'."
            )
        target_type, target_id = "task", parent

    if target_type == "task":
        _confirm(
            f"Re-run {kind} for task {target_id}?",
            yes=yes,
            json_output=json_output,
        )
        return {
            "kind": kind,
            "target": {"type": "task", "id": target_id},
            "tasks": [_task_action(api_url, target_id, kind)],
        }

    # experiment
    tasks = get_experiment_tasks(api_url, target_id) or []
    task_ids = [t.get("id") for t in tasks if t.get("id")]
    if not task_ids:
        return {
            "kind": kind,
            "target": {"type": "experiment", "id": target_id},
            "tasks": [],
            "note": "Experiment has no tasks.",
        }
    _confirm(
        f"Re-run {kind} for {len(task_ids)} task(s) in experiment {target_id}?",
        yes=yes,
        json_output=json_output,
    )
    return {
        "kind": kind,
        "target": {"type": "experiment", "id": target_id},
        "tasks": [_task_action(api_url, tid, kind) for tid in task_ids],
    }


def _report(results: dict, *, json_output: bool) -> None:
    records = results.get("trials") or results.get("tasks") or []
    failures = [r for r in records if not r.get("ok")]

    if json_output:
        results["queued"] = sum(1 for r in records if r.get("ok"))
        results["failed"] = len(failures)
        print_json(results)
        if failures:
            raise typer.Exit(1)
        return

    kind = results["kind"]
    target = results["target"]
    note = results.get("note")
    if note and not records:
        console.print(f"[yellow]{note}[/yellow]")
        return

    queued = sum(1 for r in records if r.get("ok"))
    label = "trial(s)" if kind == "trials" else f"{kind} job(s)"
    console.print(
        f"[green]Queued {queued} {label}[/green] for {target['type']} {target['id']}"
    )
    for record in failures:
        rid = record.get("trial_id") or record.get("task_id")
        console.print(
            f"[red]Failed[/red] {rid}: HTTP {record.get('status')} - "
            f"{record.get('error')}"
        )
    if failures:
        raise typer.Exit(1)

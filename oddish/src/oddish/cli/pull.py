from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()

TargetType = Literal["trial", "task", "experiment"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rel_path(path: str) -> Path:
    raw = path.replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        raise ValueError(f"Invalid path: {path}")
    parts = PurePosixPath(raw).parts
    if ".." in parts:
        raise ValueError(f"Invalid path: {path}")
    return Path(*parts)


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _get_json(
    api_url: str,
    private_path: str,
    public_path: str | None = None,
    *,
    params: dict | None = None,
) -> dict | list | None:
    headers = get_auth_headers()
    with httpx.Client(timeout=60.0, headers=headers) as client:
        response = client.get(f"{api_url}{private_path}", params=params)
    if response.status_code == 200:
        return response.json()
    if public_path:
        with httpx.Client(timeout=60.0, headers=headers) as client:
            response = client.get(f"{api_url}{public_path}", params=params)
        if response.status_code == 200:
            return response.json()
    return None


def _get_task_status(api_url: str, task_id: str) -> dict | None:
    data = _get_json(
        api_url,
        f"/tasks/{task_id}",
        f"/public/tasks/{task_id}",
    )
    if isinstance(data, dict):
        return data
    return None


def _list_trial_files(api_url: str, trial_id: str) -> dict | None:
    data = _get_json(
        api_url,
        f"/trials/{trial_id}/files",
        f"/public/trials/{trial_id}/files",
    )
    if isinstance(data, dict):
        return data
    return None


def _list_task_files(api_url: str, task_id: str) -> dict | None:
    params = {"recursive": True, "presign": False}
    data = _get_json(
        api_url,
        f"/tasks/{task_id}/files",
        f"/public/tasks/{task_id}/files",
        params=params,
    )
    if isinstance(data, dict):
        return data
    return None


def _list_tasks_for_experiment(api_url: str, experiment_id: str) -> list[dict]:
    private_data = _get_json(
        api_url,
        "/tasks",
        None,
        params={"experiment_id": experiment_id},
    )
    if isinstance(private_data, list) and private_data:
        return private_data

    public_experiments = _get_json(api_url, "/public/experiments", "/public/experiments")
    if not isinstance(public_experiments, list):
        return []
    public_token = None
    for exp in public_experiments:
        if isinstance(exp, dict) and exp.get("id") == experiment_id:
            token = exp.get("public_token")
            if isinstance(token, str) and token:
                public_token = token
                break
    if not public_token:
        return []

    data = _get_json(
        api_url,
        f"/public/experiments/{public_token}/tasks",
        f"/public/experiments/{public_token}/tasks",
    )
    if isinstance(data, list):
        return data
    return []


def _download_trial_file(
    api_url: str,
    trial_id: str,
    remote_path: str,
) -> tuple[bytes | None, str | None]:
    encoded_path = quote(remote_path, safe="/")
    headers = get_auth_headers()
    with httpx.Client(timeout=60.0, headers=headers) as client:
        response = client.get(f"{api_url}/trials/{trial_id}/files/{encoded_path}")
    if response.status_code != 200:
        with httpx.Client(timeout=60.0, headers=headers) as client:
            response = client.get(
                f"{api_url}/public/trials/{trial_id}/files/{encoded_path}"
            )
    if response.status_code != 200:
        return None, f"{response.status_code}: {response.text}"
    return response.content, None


def _download_task_file(
    api_url: str,
    task_id: str,
    remote_path: str,
) -> tuple[str | None, str | None]:
    encoded_path = quote(remote_path, safe="/")
    headers = get_auth_headers()
    params = {"presign": False}
    with httpx.Client(timeout=60.0, headers=headers) as client:
        response = client.get(
            f"{api_url}/tasks/{task_id}/files/{encoded_path}",
            params=params,
        )
    if response.status_code != 200:
        with httpx.Client(timeout=60.0, headers=headers) as client:
            response = client.get(
                f"{api_url}/public/tasks/{task_id}/files/{encoded_path}",
                params=params,
            )
    if response.status_code != 200:
        return None, f"{response.status_code}: {response.text}"
    data = response.json()
    return str(data.get("content", "")), None


def _pull_trial(
    api_url: str,
    trial_id: str,
    output_root: Path,
    *,
    include_logs: bool,
    include_files: bool,
    include_structured_logs: bool,
) -> dict:
    trial_root = output_root / "trials" / trial_id
    summary: dict[str, int | str] = {
        "trial_id": trial_id,
        "logs_saved": 0,
        "files_saved": 0,
        "files_skipped": 0,
        "errors": 0,
    }

    if include_logs:
        logs_payload = _get_json(
            api_url,
            f"/trials/{trial_id}/logs",
            f"/public/trials/{trial_id}/logs",
        )
        if isinstance(logs_payload, dict):
            _write_text(trial_root / "logs.txt", logs_payload.get("logs", ""))
            summary["logs_saved"] = int(summary["logs_saved"]) + 1
        else:
            summary["errors"] = int(summary["errors"]) + 1

        if include_structured_logs:
            structured_payload = _get_json(
                api_url,
                f"/trials/{trial_id}/logs/structured",
                f"/public/trials/{trial_id}/logs/structured",
            )
            if isinstance(structured_payload, dict):
                _write_json(trial_root / "logs_structured.json", structured_payload)
                summary["logs_saved"] = int(summary["logs_saved"]) + 1
            else:
                summary["errors"] = int(summary["errors"]) + 1

    result_payload = _get_json(
        api_url,
        f"/trials/{trial_id}/result",
        f"/public/trials/{trial_id}/result",
    )
    if isinstance(result_payload, dict):
        _write_json(trial_root / "result.json", result_payload)
    trajectory_payload = _get_json(
        api_url,
        f"/trials/{trial_id}/trajectory",
        f"/public/trials/{trial_id}/trajectory",
    )
    if isinstance(trajectory_payload, dict):
        _write_json(trial_root / "trajectory.json", trajectory_payload)

    if include_files:
        listing = _list_trial_files(api_url, trial_id)
        if listing:
            for file_meta in listing.get("files", []):
                remote_path = file_meta.get("path")
                if not remote_path:
                    continue
                try:
                    rel = _safe_rel_path(remote_path)
                except ValueError:
                    summary["errors"] = int(summary["errors"]) + 1
                    continue
                local_file = trial_root / "files" / rel
                remote_size = file_meta.get("size")
                if (
                    local_file.exists()
                    and local_file.is_file()
                    and isinstance(remote_size, int)
                    and local_file.stat().st_size == remote_size
                ):
                    summary["files_skipped"] = int(summary["files_skipped"]) + 1
                    continue
                content, err = _download_trial_file(api_url, trial_id, remote_path)
                if content is None:
                    summary["errors"] = int(summary["errors"]) + 1
                    if err:
                        _write_text(
                            trial_root / "errors" / f"{rel.as_posix()}.error.txt", err
                        )
                    continue
                _write_bytes(local_file, content)
                summary["files_saved"] = int(summary["files_saved"]) + 1

    return summary


def _pull_task_files(api_url: str, task_id: str, output_root: Path) -> dict:
    task_root = output_root / "tasks" / task_id / "files"
    summary = {"task_files_saved": 0, "task_files_skipped": 0, "task_file_errors": 0}
    listing = _list_task_files(api_url, task_id)
    if not listing:
        return summary

    for file_meta in listing.get("files", []):
        remote_path = file_meta.get("path")
        if not remote_path:
            continue
        try:
            rel = _safe_rel_path(remote_path)
        except ValueError:
            summary["task_file_errors"] += 1
            continue

        local_file = task_root / rel
        remote_size = file_meta.get("size")
        if (
            local_file.exists()
            and local_file.is_file()
            and isinstance(remote_size, int)
            and local_file.stat().st_size == remote_size
        ):
            summary["task_files_skipped"] += 1
            continue

        content, err = _download_task_file(api_url, task_id, remote_path)
        if content is None:
            summary["task_file_errors"] += 1
            if err:
                _write_text(task_root / "errors" / f"{rel.as_posix()}.error.txt", err)
            continue
        _write_text(local_file, content)
        summary["task_files_saved"] += 1

    return summary


def _trial_task_id(trial_id: str) -> str | None:
    task_id, sep, maybe_index = trial_id.rpartition("-")
    if not sep:
        return None
    if not maybe_index.isdigit():
        return None
    return task_id or None


def _resolve_target(api_url: str, value: str, kind: TargetType | None) -> tuple[TargetType, str]:
    if kind:
        return kind, value

    trial_task_id = _trial_task_id(value)
    if trial_task_id:
        task = _get_task_status(api_url, trial_task_id)
        if task and any(t.get("id") == value for t in task.get("trials", []) or []):
            return "trial", value

    task = _get_task_status(api_url, value)
    if task:
        return "task", value

    experiment_tasks = _list_tasks_for_experiment(api_url, value)
    if experiment_tasks:
        return "experiment", value

    raise typer.BadParameter(f"Unable to resolve '{value}' as trial, task, or experiment.")


def _is_trial_terminal(api_url: str, trial_id: str) -> bool:
    task_id = _trial_task_id(trial_id)
    if not task_id:
        return True
    task = _get_task_status(api_url, task_id)
    if not task:
        return False
    trials = task.get("trials", []) or []
    for trial in trials:
        if trial.get("id") == trial_id:
            return trial.get("status") in ("success", "failed")
    return False


def _is_task_terminal(api_url: str, task_id: str) -> bool:
    task = _get_task_status(api_url, task_id)
    if not task:
        return False
    return task.get("status") in ("completed", "failed")


def _is_experiment_terminal(api_url: str, experiment_id: str) -> bool:
    tasks = _list_tasks_for_experiment(api_url, experiment_id)
    if not tasks:
        return True
    return all(t.get("status") in ("completed", "failed") for t in tasks)


def _pull_once(
    api_url: str,
    target_type: TargetType,
    target_id: str,
    output_root: Path,
    *,
    include_logs: bool,
    include_files: bool,
    include_structured_logs: bool,
    include_task_files: bool,
) -> dict:
    run_manifest: dict = {
        "target_type": target_type,
        "target_id": target_id,
        "pulled_at": _utc_now(),
        "trials": [],
        "tasks": [],
        "errors": [],
    }

    if target_type == "trial":
        summary = _pull_trial(
            api_url,
            target_id,
            output_root,
            include_logs=include_logs,
            include_files=include_files,
            include_structured_logs=include_structured_logs,
        )
        run_manifest["trials"].append(summary)
        return run_manifest

    if target_type == "task":
        task = _get_task_status(api_url, target_id)
        if not task:
            raise typer.BadParameter(f"Task '{target_id}' not found.")
        _write_json(output_root / "tasks" / target_id / "task.json", task)
        run_manifest["tasks"].append(
            {"task_id": target_id, "status": task.get("status"), "experiment_id": task.get("experiment_id")}
        )
        for trial in task.get("trials", []) or []:
            trial_id = trial.get("id")
            if not trial_id:
                continue
            run_manifest["trials"].append(
                _pull_trial(
                    api_url,
                    trial_id,
                    output_root,
                    include_logs=include_logs,
                    include_files=include_files,
                    include_structured_logs=include_structured_logs,
                )
            )
        if include_task_files and include_files:
            run_manifest["tasks"][-1] |= _pull_task_files(api_url, target_id, output_root)
        return run_manifest

    tasks = _list_tasks_for_experiment(api_url, target_id)
    if not tasks:
        raise typer.BadParameter(f"Experiment '{target_id}' not found or has no tasks.")

    for task in tasks:
        task_id = task.get("id")
        if not task_id:
            continue
        full_task = _get_task_status(api_url, task_id) or task
        _write_json(output_root / "tasks" / task_id / "task.json", full_task)
        task_summary: dict = {
            "task_id": task_id,
            "status": full_task.get("status"),
            "experiment_id": full_task.get("experiment_id"),
        }
        for trial in full_task.get("trials", []) or []:
            trial_id = trial.get("id")
            if not trial_id:
                continue
            run_manifest["trials"].append(
                _pull_trial(
                    api_url,
                    trial_id,
                    output_root,
                    include_logs=include_logs,
                    include_files=include_files,
                    include_structured_logs=include_structured_logs,
                )
            )
        if include_task_files and include_files:
            task_summary |= _pull_task_files(api_url, task_id, output_root)
        run_manifest["tasks"].append(task_summary)

    return run_manifest


def pull(
    target: Annotated[
        str,
        typer.Argument(help="Trial ID, task ID, or experiment ID to pull."),
    ],
    target_type: Annotated[
        TargetType | None,
        typer.Option(
            "--type",
            help="Force target type instead of auto-resolving.",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help="Output directory (default: ./oddish-pulls/<target>).",
        ),
    ] = None,
    logs: Annotated[
        bool,
        typer.Option("--logs/--no-logs", help="Pull trial logs."),
    ] = True,
    files: Annotated[
        bool,
        typer.Option("--files/--no-files", help="Pull trial/task artifact files."),
    ] = True,
    structured: Annotated[
        bool,
        typer.Option("--structured", help="Also save structured trial logs."),
    ] = False,
    include_task_files: Annotated[
        bool,
        typer.Option(
            "--include-task-files",
            help="Include task-level files when target is task/experiment.",
        ),
    ] = False,
    watch: Annotated[
        bool,
        typer.Option("--watch", "-w", help="Keep pulling while run is in progress."),
    ] = False,
    interval: Annotated[
        int,
        typer.Option("--interval", help="Polling interval in seconds for --watch."),
    ] = 5,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
):
    """Pull logs and artifacts from Oddish remote to local files."""
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    if interval < 1:
        raise typer.BadParameter("--interval must be >= 1")

    resolved_type, resolved_id = _resolve_target(api_url, target, target_type)
    output_root = out or (Path.cwd() / "oddish-pulls" / resolved_id)
    output_root.mkdir(parents=True, exist_ok=True)

    console.print(
        f"[cyan]Pulling[/cyan] type={resolved_type} id={resolved_id} -> {output_root}"
    )

    iteration = 0
    while True:
        iteration += 1
        run_manifest = _pull_once(
            api_url,
            resolved_type,
            resolved_id,
            output_root,
            include_logs=logs,
            include_files=files,
            include_structured_logs=structured,
            include_task_files=include_task_files,
        )
        manifest = {
            "source": {"api_url": api_url, "target_type": resolved_type, "target_id": resolved_id},
            "pulled_at": _utc_now(),
            "watch": watch,
            "watch_iteration": iteration,
            "run": run_manifest,
        }
        _write_json(output_root / "manifest.json", manifest)

        total_saved = sum(
            int(t.get("files_saved", 0)) + int(t.get("logs_saved", 0))
            for t in run_manifest.get("trials", [])
        )
        console.print(
            f"[green]Pull iteration {iteration} complete[/green] "
            f"({len(run_manifest.get('trials', []))} trials, {total_saved} artifacts/log files saved)"
        )

        if not watch:
            break

        if resolved_type == "trial":
            done = _is_trial_terminal(api_url, resolved_id)
        elif resolved_type == "task":
            done = _is_task_terminal(api_url, resolved_id)
        else:
            done = _is_experiment_terminal(api_url, resolved_id)

        if done:
            console.print("[green]Target reached terminal state; stopping watch.[/green]")
            break

        console.print(
            f"[dim]Target still running; polling again in {interval}s...[/dim]"
        )
        time.sleep(interval)


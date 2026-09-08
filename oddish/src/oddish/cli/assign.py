"""Assign a batch of task versions to a person for QA review."""

from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, get_auth_headers, print_json, require_api_key

console = Console()
error_console = Console(stderr=True)


def assign(
    assignee: Annotated[
        str, typer.Option("--to", help="Org member's email, user ID, or GitHub handle.")
    ],
    task_ids: Annotated[
        list[str] | None, typer.Argument(help="Task IDs to assign for QA review.")
    ] = None,
    tasks_file: Annotated[
        Path | None,
        typer.Option(
            "--tasks-file",
            exists=True,
            dir_okay=False,
            help="Text file of whitespace-separated task IDs.",
        ),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace", help="Reassign tasks already owned by another person."
        ),
    ] = False,
    api_url: Annotated[str, typer.Option("--api", help="API URL")] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Output JSON for scripts.")
    ] = False,
) -> None:
    """Assign QA review ownership by task ID, without a delivery ID.

    Writes the owner shown on active delivery boards. Existing owners are
    skipped unless --replace is set. Accepts up to 1,000 unique task IDs.
    """
    ids = list(task_ids or [])
    if tasks_file is not None:
        try:
            ids.extend(tasks_file.read_text().split())
        except (OSError, UnicodeError) as exc:
            error_console.print(f"Cannot read task IDs: {exc}", markup=False)
            raise typer.Exit(1) from exc
    ids = list(dict.fromkeys(ids))
    if not 1 <= len(ids) <= 1000:
        raise typer.BadParameter(
            "Provide 1–1,000 unique task IDs as arguments or with --tasks-file."
        )
    if not assignee.strip():
        raise typer.BadParameter("Assignee must not be empty", param_hint="--to")
    api_url = api_url or get_api_url()
    require_api_key(api_url)
    try:
        with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
            response = client.post(
                f"{api_url.rstrip('/')}/tasks/qa-work/assign",
                json={"task_ids": ids, "assignee": assignee, "replace": replace},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except ValueError:
            detail = exc.response.text
        error_console.print(
            f"Assignment failed ({exc.response.status_code}): {detail}", markup=False
        )
        raise typer.Exit(1) from exc
    except httpx.RequestError as exc:
        error_console.print(f"Assignment request failed: {exc}", markup=False)
        raise typer.Exit(1) from exc

    result = response.json()
    if json_output:
        print_json(result)
        return
    console.print(
        f"Assigned {len(result['assigned_task_ids'])} tasks to {result['owner_user_id']}; "
        f"{len(result['unchanged_task_ids'])} already assigned to this person; "
        f"{len(result['skipped_task_ids'])} owned by someone else.",
        markup=False,
    )
    for task_id in result["skipped_task_ids"]:
        console.print(f"Skipped: {task_id}", markup=False)
    if result["skipped_task_ids"]:
        console.print("Use --replace to reassign those tasks.")

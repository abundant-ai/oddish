from __future__ import annotations

from typing import Annotated, Callable, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli._collection_refs import _dedupe, expand_task_refs
from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    require_api_key,
)

console = Console()
experiment_app = typer.Typer(help="Manage experiments.", no_args_is_help=True)


def _normalize_trial_ids(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in raw if s and s.strip()))


def _format_collection_summary(data: dict) -> list[str]:
    return [
        f"[green]Created collection {data.get('id')}[/green] ({data.get('name')})",
        f"  Trials linked: {data.get('trials_linked', 0)}",
        f"  Tasks linked:  {data.get('tasks_linked', 0)}",
    ]


@experiment_app.command("create")
def create(
    trial_ids: Annotated[
        list[str],
        typer.Argument(help="Trial IDs to gather (space-separated)."),
    ],
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Name for the collection experiment."),
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the raw JSON response.")
    ] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Gather existing trials into a new read-only collection experiment.

    The trials keep their home experiment; the collection just references
    them for viewing in the dashboard.

        oddish experiment create --name "my collection" trial_a trial_b trial_c
    """
    ids = _normalize_trial_ids(trial_ids)
    if not ids:
        console.print("[red]Provide at least one trial id.[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        resp = client.post(
            f"{api_url}/experiments/collections",
            json={"name": name, "trial_ids": ids},
        )
    if resp.status_code != 200:
        console.print(f"[red]Failed:[/red] {resp.text}")
        raise typer.Exit(1)

    data = resp.json()
    if json_output:
        import json as _json

        console.print_json(_json.dumps(data))
        return
    for line in _format_collection_summary(data):
        console.print(line)


def _share_or_dashboard_url(client, api_url: str, experiment_id: str) -> str:
    """The public share URL if the collection is published, else its dashboard
    URL. A failed lookup is not fatal -- the mutation already succeeded."""
    dashboard = f"{get_dashboard_url(api_url)}/experiments/{experiment_id}"
    try:
        resp = client.get(f"{api_url}/experiments/{experiment_id}/share")
        if resp.status_code != 200:
            return dashboard
        token = resp.json().get("public_token")
    except Exception:
        return dashboard
    return f"{get_dashboard_url(api_url)}/share/{token}" if token else dashboard


_TASKS_SCOPE_HINT = "This action requires a TASKS-scoped API key."
_ADMIN_SCOPE_HINT = (
    "This action requires an admin API key, the same gate `oddish delete` uses."
)


def _explain_failure(resp, permission_hint: str) -> None:
    """Turn the API's error codes into something actionable."""
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    if resp.status_code == 409 and "not a collection" in str(detail):
        console.print(f"[red]{detail}[/red]")
        console.print(
            "  Only read-only collections can be edited. Create one with "
            "[bold]oddish collect[/bold]."
        )
    elif resp.status_code == 409:
        console.print(f"[red]{detail}[/red]")
    elif resp.status_code == 403:
        console.print(f"[red]Not permitted:[/red] {detail}")
        console.print(f"  {permission_hint}")
    elif resp.status_code == 404:
        console.print(f"[red]Not found:[/red] {detail}")
    else:
        console.print(f"[red]Failed:[/red] {resp.status_code} - {resp.text}")


def _print_mutation(data: dict, url: str) -> None:
    console.print(
        f"[green]Updated collection {data.get('id')}[/green] ({data.get('name')})"
    )
    if data.get("trials_added"):
        console.print(f"  Trials added:   {data['trials_added']}")
    if data.get("trials_removed"):
        console.print(f"  Trials removed: {data['trials_removed']}")
    if data.get("tasks_linked"):
        console.print(f"  Tasks linked:   {data['tasks_linked']}")
    if data.get("tasks_unlinked"):
        console.print(f"  Tasks unlinked: {data['tasks_unlinked']}")
    console.print(f"  Now contains:   {data.get('trials_total', 0)} trials")
    console.print(f"  View:           {url}")


def _run_collection_mutation(
    client: httpx.Client,
    issue_request: Callable[[], httpx.Response],
    *,
    payload: dict,
    empty_payload_message: Optional[str],
    permission_hint: str,
    api_url: str,
    experiment_id: str,
    json_output: bool,
) -> None:
    """Shared plumbing for add/remove/rename: empty-payload guard, the
    request itself, error handling, and the success summary. `issue_request`
    captures the command-specific HTTP verb/URL (e.g. `client.post(...)`)
    so test doubles that only implement one verb keep working.
    """
    if empty_payload_message is not None and not any(payload.values()):
        console.print(f"[red]{empty_payload_message}[/red]")
        raise typer.Exit(1)

    try:
        resp = issue_request()
    except httpx.RequestError as e:
        console.print(f"[red]Failed to connect to API:[/red] {e}")
        raise typer.Exit(1)
    if resp.status_code != 200:
        _explain_failure(resp, permission_hint)
        raise typer.Exit(1)
    data = resp.json()
    url = _share_or_dashboard_url(client, api_url, experiment_id)

    if json_output:
        console.print_json(data={**data, "url": url})
        return
    _print_mutation(data, url)


@experiment_app.command("add")
def add(
    experiment_id: Annotated[
        str, typer.Argument(help="Collection experiment id to add to.")
    ],
    trial_ids: Annotated[
        Optional[list[str]],
        typer.Argument(help="Trial IDs to add (optional; combine with --task/--from)."),
    ] = None,
    tasks: Annotated[
        Optional[list[str]],
        typer.Option(
            "--task",
            "-t",
            help=(
                "Task id/name; adds its current-version trials. Repeatable. "
                "Append @<version> (e.g. mytask@16) for that version instead."
            ),
        ),
    ] = None,
    from_experiments: Annotated[
        Optional[list[str]],
        typer.Option(
            "--from",
            help="Experiment id whose trials to merge in. Repeatable.",
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print raw JSON.")
    ] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Add trials to an existing read-only collection, keeping its share link.

        oddish experiment add bd43dc73 --from 83a0b745
        oddish experiment add bd43dc73 trial_a trial_b --task mytask@16
    """
    trial_ids = _dedupe(trial_ids or [])
    tasks = tasks or []
    from_experiments = _dedupe(from_experiments or [])
    if not trial_ids and not tasks and not from_experiments:
        console.print(
            "[red]Provide at least one trial id, --task, or --from.[/red]"
        )
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    with httpx.Client(timeout=120.0, headers=get_auth_headers()) as client:
        plain_tasks, pinned_trial_ids = expand_task_refs(client, api_url, tasks)
        payload = {
            "trial_ids": _dedupe([*trial_ids, *pinned_trial_ids]),
            "task_ids": plain_tasks,
            "from_experiment_ids": from_experiments,
        }
        url_path = f"{api_url}/experiments/{experiment_id}/collection/trials"
        _run_collection_mutation(
            client,
            lambda: client.post(url_path, json=payload),
            payload=payload,
            empty_payload_message=(
                "Nothing to add: every --task pin resolved to zero "
                "linkable trials."
            ),
            permission_hint=_TASKS_SCOPE_HINT,
            api_url=api_url,
            experiment_id=experiment_id,
            json_output=json_output,
        )


@experiment_app.command("remove")
def remove(
    experiment_id: Annotated[
        str, typer.Argument(help="Collection experiment id to remove from.")
    ],
    trial_ids: Annotated[
        Optional[list[str]],
        typer.Argument(help="Trial IDs to remove (optional; combine with --task)."),
    ] = None,
    tasks: Annotated[
        Optional[list[str]],
        typer.Option(
            "--task",
            "-t",
            help=(
                "Task id/name; removes ALL of its trials from the collection, "
                "every version. Append @<version> to remove just that version. "
                "Repeatable."
            ),
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print raw JSON.")
    ] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Remove trials from a read-only collection, keeping its share link.

    The trials themselves are untouched -- they stay in their home experiment
    with their artifacts. Only the collection's view of them changes.

        oddish experiment remove bd43dc73 --task cobol-batch-java-parity-9019af79
        oddish experiment remove bd43dc73 trial_a trial_b --yes
    """
    trial_ids = _dedupe(trial_ids or [])
    tasks = tasks or []
    if not trial_ids and not tasks:
        console.print("[red]Provide at least one trial id or --task.[/red]")
        raise typer.Exit(1)

    if not yes:
        target = ", ".join([*trial_ids, *(f"task {t}" for t in tasks)])
        # --json never implies consent; scripted use needs --yes --json.
        if not typer.confirm(f"Remove {target} from collection {experiment_id}?"):
            console.print("Aborted.")
            raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    with httpx.Client(timeout=120.0, headers=get_auth_headers()) as client:
        plain_tasks, pinned_trial_ids = expand_task_refs(client, api_url, tasks)
        payload = {
            "trial_ids": _dedupe([*trial_ids, *pinned_trial_ids]),
            "task_ids": plain_tasks,
        }
        url_path = f"{api_url}/experiments/{experiment_id}/collection/trials"
        _run_collection_mutation(
            client,
            lambda: client.request("DELETE", url_path, json=payload),
            payload=payload,
            empty_payload_message=(
                "Nothing to remove: every --task pin resolved to zero trials."
            ),
            permission_hint=_ADMIN_SCOPE_HINT,
            api_url=api_url,
            experiment_id=experiment_id,
            json_output=json_output,
        )


@experiment_app.command("rename")
def rename(
    experiment_id: Annotated[
        str, typer.Argument(help="Collection experiment id to rename.")
    ],
    name: Annotated[
        str, typer.Option("--name", "-n", help="New name for the collection.")
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Print raw JSON.")
    ] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Rename a read-only collection. The share link is unaffected.

        oddish experiment rename bd43dc73 -n "21-task rollup"
    """
    stripped = (name or "").strip()
    if not stripped:
        console.print("[red]Name must not be empty.[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        url_path = f"{api_url}/experiments/{experiment_id}/collection"
        payload = {"name": stripped}
        _run_collection_mutation(
            client,
            lambda: client.patch(url_path, json=payload),
            payload=payload,
            empty_payload_message=None,
            permission_hint=_ADMIN_SCOPE_HINT,
            api_url=api_url,
            experiment_id=experiment_id,
            json_output=json_output,
        )

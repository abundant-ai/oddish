from __future__ import annotations

from typing import Annotated, Callable, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    require_api_key,
)

# Re-exported so `from oddish.cli.collect import _parse_task_ref` keeps working.
from oddish.cli._collection_refs import (  # noqa: F401
    _BROWSE_MAX_SCAN,
    _BROWSE_PAGE,
    _LINKABLE_STATUSES,
    _dedupe,
    _parse_task_ref,
    _resolve_task_id,
    _version_trial_ids,
    expand_task_refs,
)

console = Console()


def _guard_sources(*, tasks: list[str], trial_ids: list[str]) -> bool:
    return bool(_dedupe(tasks) or _dedupe(trial_ids))


def _build_payload(*, name: str, tasks: list[str], trial_ids: list[str]) -> dict:
    return {"name": name, "task_ids": _dedupe(tasks), "trial_ids": _dedupe(trial_ids)}


def _share_url(api_url: str, public_token: str | None) -> str | None:
    if not public_token:
        return None
    return f"{get_dashboard_url(api_url)}/share/{public_token}"


def _call(
    send: Callable[[], httpx.Response],
    *,
    label: str,
    hint_403: str | None = None,
    note_on_failure: str | None = None,
) -> dict:
    """POST/PATCH and return the JSON body, exiting on any failure.

    ``note_on_failure`` reports work that already committed server-side before
    this call failed, so a partial success is never silently discarded.
    """
    try:
        resp = send()
    except httpx.RequestError as e:
        console.print(f"[red]Failed to connect to API:[/red] {e}")
        if note_on_failure:
            console.print(f"  {note_on_failure}")
        raise typer.Exit(1)
    if resp.status_code != 200:
        console.print(f"[red]{label} failed:[/red] {resp.status_code} - {resp.text}")
        if resp.status_code == 403 and hint_403:
            console.print(f"  {hint_403}")
        if note_on_failure:
            console.print(f"  {note_on_failure}")
        raise typer.Exit(1)
    return resp.json()


def _mutate_collection(
    *,
    api_url: str,
    experiment_id: str,
    name: str,
    tasks: list[str],
    trial_ids: list[str],
    json_output: bool,
) -> None:
    added: dict | None = None
    renamed: dict | None = None
    append_failed = False

    with httpx.Client(timeout=120.0, headers=get_auth_headers()) as client:
        plain_tasks, pinned_trial_ids = expand_task_refs(client, api_url, tasks)
        resolved_trial_ids = [*trial_ids, *pinned_trial_ids]

        wanted_sources = _guard_sources(tasks=tasks, trial_ids=trial_ids)
        have_sources = _guard_sources(tasks=plain_tasks, trial_ids=resolved_trial_ids)
        if wanted_sources and not have_sources:
            console.print(
                "[red]Nothing to append: every --task pin resolved to zero "
                "linkable trials.[/red]"
            )
            # Empty pins say nothing about the rename, so fall through and let
            # --name still land; the exit code below still reports the failure.
            if not name:
                raise typer.Exit(1)
            append_failed = True

        if have_sources:
            added = _call(
                lambda: client.post(
                    f"{api_url}/experiments/{experiment_id}/collection/trials",
                    json={
                        "trial_ids": _dedupe(resolved_trial_ids),
                        "task_ids": _dedupe(plain_tasks),
                        "from_experiment_ids": [],
                    },
                ),
                label="Append",
                hint_403="Appending requires a TASKS-scope API key.",
            )

        # Rename last: it needs admin while the append above only needs TASKS,
        # so a TASKS-scoped key still lands its trials before the 403.
        if name:
            renamed = _call(
                lambda: client.patch(
                    f"{api_url}/experiments/{experiment_id}/collection",
                    json={"name": name},
                ),
                label="Rename",
                hint_403=(
                    "Renaming requires an admin API key; a TASKS-scoped key can "
                    "append but not rename."
                ),
                note_on_failure=(
                    f"The collection was NOT renamed, but "
                    f"{added.get('trials_added', 0)} trial(s) were already "
                    f"appended and remain linked."
                    if added
                    else None
                ),
            )

    # A rename response carries zeroed counters; keep the append's and take
    # only the new name from it.
    if added and renamed:
        data = {**added, "name": renamed["name"]}
    else:
        data = added or renamed

    if json_output:
        console.print_json(data=data)
        if append_failed:
            raise typer.Exit(1)
        return

    console.print(
        f"[green]Updated collection {data.get('id')}[/green] ({data.get('name')})"
    )
    if added:
        console.print(f"  Trials added:       {added.get('trials_added', 0)}")
        console.print(f"  Trials now linked:  {added.get('trials_total', 0)}")
        console.print(f"  Tasks linked:       {added.get('tasks_linked', 0)}")
    if renamed:
        console.print(f"  Renamed to:         {renamed.get('name')}")
    console.print(f"  View: {get_dashboard_url(api_url)}/experiments/{data.get('id')}")

    if append_failed:
        raise typer.Exit(1)


def collect(
    trial_ids: Annotated[
        Optional[list[str]],
        typer.Argument(help="Trial IDs to include (optional; combine with --task)."),
    ] = None,
    tasks: Annotated[
        Optional[list[str]],
        typer.Option(
            "--task",
            "-t",
            help=(
                "Task id/name; links its current-version trials. Repeatable. "
                "Append @<version> (e.g. mytask@16) to link that version's "
                "trials instead of the current one."
            ),
        ),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("--name", "-n", help="Name for the collection."),
    ] = None,
    into: Annotated[
        Optional[str],
        typer.Option(
            "--into",
            help=(
                "Existing collection id to edit instead of creating a new one; "
                "appends the given trials/tasks and/or renames it to --name."
            ),
        ),
    ] = None,
    publish: Annotated[
        bool,
        typer.Option(
            "--publish/--no-publish",
            help="Publish a public read-only link (default: publish).",
        ),
    ] = True,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print raw JSON.")
    ] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Gather the latest trials of one or more tasks into a read-only collection.

    oddish collect --task activiti-spring-boot-3-upgrade --task struts-rest-showcase-to-spring-mvc
    oddish collect --task my-task --no-publish -n "my rollup"
    oddish collect --task my-task --task spring-petclinic@16   # pin one task to v16
    oddish collect --into 2c7973d8 --task my-task -n "new name" # edit an existing one
    """
    tasks = tasks or []
    trial_ids = trial_ids or []
    has_sources = _guard_sources(tasks=tasks, trial_ids=trial_ids)
    if into:
        if not has_sources and not (name or "").strip():
            console.print(
                "[red]Nothing to do: --into needs --name and/or trials/--task.[/red]"
            )
            raise typer.Exit(1)
    elif not has_sources:
        console.print("[red]Provide at least one --task or trial id.[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    if into:
        # No publish here: the collection exists and may already be shared.
        _mutate_collection(
            api_url=api_url,
            experiment_id=into.strip(),
            name=(name or "").strip(),
            tasks=tasks,
            trial_ids=trial_ids,
            json_output=json_output,
        )
        return

    coll_name = (name or "").strip() or "collection"

    public_url = None
    public_token = None
    publish_failed = False
    publish_status = None

    with httpx.Client(timeout=120.0, headers=get_auth_headers()) as client:
        plain_tasks, pinned_trial_ids = expand_task_refs(client, api_url, tasks)

        resolved_trial_ids = [*trial_ids, *pinned_trial_ids]
        # Re-guard: the initial check ran before pins were expanded, so a set of
        # pins that all resolve to zero trials would otherwise post an empty
        # payload and hit the server's 400.
        if not _guard_sources(tasks=plain_tasks, trial_ids=resolved_trial_ids):
            console.print(
                "[red]Nothing to collect: every --task pin resolved to zero "
                "linkable trials.[/red]"
            )
            raise typer.Exit(1)

        payload = _build_payload(
            name=coll_name,
            tasks=plain_tasks,
            trial_ids=resolved_trial_ids,
        )

        data = _call(
            lambda: client.post(f"{api_url}/experiments/collections", json=payload),
            label="Collect",
        )

        if publish and data.get("id"):
            pub = client.post(f"{api_url}/experiments/{data['id']}/publish")
            if pub.status_code == 200:
                # ExperimentShareResponse returns public_token, not a full URL.
                public_token = pub.json().get("public_token")
                public_url = _share_url(api_url, public_token)
            else:
                publish_failed = True
                publish_status = pub.status_code

    if json_output:
        console.print_json(
            data={**data, "public_token": public_token, "public_url": public_url}
        )
        if publish_failed:
            raise typer.Exit(1)
        return

    console.print(
        f"[green]Created collection {data.get('id')}[/green] ({data.get('name')})"
    )
    console.print(f"  Trials linked:      {data.get('trials_linked', 0)}")
    console.print(f"  From tasks:         {data.get('trials_from_tasks', 0)}")
    skipped = data.get("tasks_skipped_empty", 0)
    if skipped:
        console.print(f"  Tasks skipped (empty): {skipped}")

    if public_url:
        console.print("[bold]This is a public, read-only link:[/bold]")
        console.print(f"  {public_url}")
    elif publish_failed:
        # Publish route requires a FULL-scope key while create only needs TASKS,
        # so a TASKS-scoped key creates the collection but cannot publish it.
        console.print(
            f"[red]Collection created but NOT published (it is private).[/red] "
            f"Publish returned HTTP {publish_status}."
        )
        if publish_status == 403:
            console.print(
                "  Publishing requires a FULL-scope API key; your key may be TASKS-scoped."
            )
        console.print(
            f"  View (private): {get_dashboard_url(api_url)}/experiments/{data.get('id')}"
        )
        raise typer.Exit(1)
    else:
        exp_id = data.get("id")
        if exp_id:
            console.print(f"  View: {get_dashboard_url(api_url)}/experiments/{exp_id}")

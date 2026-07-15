from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    require_api_key,
)

console = Console()


def _dedupe(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in raw if s and s.strip()))


def _guard_sources(*, tasks: list[str], trial_ids: list[str]) -> bool:
    return bool(_dedupe(tasks) or _dedupe(trial_ids))


# Terminal, linkable trial statuses — mirror create_trial_collection_core's
# _TERMINAL so a version-pinned `--task name@N` links the same set the server
# would for the current version.
_LINKABLE_STATUSES = frozenset({"success", "failed", "skipped"})

# /tasks/browse caps limit at 100. Scan a bounded number of pages for an exact
# name match; a needle matching more tasks than this is too vague to pin.
_BROWSE_PAGE = 100
_BROWSE_MAX_SCAN = 500


def _parse_task_ref(ref: str) -> tuple[str, str | None]:
    """Split ``name@version`` into ``(name, version)``.

    ``spring-petclinic@16`` -> ``("spring-petclinic", "16")``.
    A bare ``name`` (no ``@``) -> ``("name", None)`` and links the current
    version server-side, as before. The version is normalized to its digits so
    ``@v16``, ``@16`` and ``@016`` are equivalent -- version ids are built as
    ``{task_id}-v{int}``, so a leading zero would otherwise never match.
    """
    base, sep, ver = ref.rpartition("@")
    if not sep:
        return ref.strip(), None
    ver = ver.strip().lstrip("vV").strip()
    base = base.strip()
    if not base or not ver.isdigit():
        # Not a version suffix (e.g. an email-ish id); treat whole thing as name.
        return ref.strip(), None
    return base, str(int(ver))


def _resolve_task_id(client, api_url: str, ref: str) -> str | None:
    """Resolve a task id or name to a concrete task id.

    Mirrors the server's `or_(TaskModel.id == ident, TaskModel.name == ident)` so
    a pinned `ref@N` resolves the same task a bare `--task ref` would. The id
    lookup must come first: /tasks/browse matches only name/author/tags, never
    id, so an exact task id is invisible to it.
    """
    resp = client.get(f"{api_url}/tasks/{ref}", params={"include_trials": "false"})
    if resp.status_code == 200:
        return resp.json().get("id") or ref
    if resp.status_code != 404:
        # Only a 404 means "not an id, try a name". Anything else is a real
        # failure and must not be reported as an unknown task.
        console.print(
            f"[red]Failed to look up task '{ref}' "
            f"(HTTP {resp.status_code}).[/red]"
        )
        raise typer.Exit(1)
    # browse matches the needle as a *substring*, so the exact name can sit
    # outside the first page when many tasks share it. Page through rather than
    # give up early and fail a pin that a bare --task would have resolved.
    # Quote the needle: browse runs it through parse_search_query, where a
    # leading `-` would otherwise become an exclusion instead of a literal.
    offset = 0
    while offset < _BROWSE_MAX_SCAN:
        resp = client.get(
            f"{api_url}/tasks/browse",
            params={
                "query": f'"{ref}"',
                "limit": _BROWSE_PAGE,
                "offset": offset,
            },
        )
        if resp.status_code != 200:
            console.print(
                f"[red]Task search failed for '{ref}' "
                f"(HTTP {resp.status_code}).[/red]"
            )
            raise typer.Exit(1)
        items = resp.json().get("items", [])
        for it in items:
            if it.get("id") == ref or it.get("name") == ref:
                return it.get("id")
        if len(items) < _BROWSE_PAGE:
            return None
        offset += _BROWSE_PAGE
    return None


def _version_trial_ids(
    client, api_url: str, task_id: str, version: str
) -> list[str] | None:
    """Trial ids of a task's given version: terminal, non-probe, non-superseded.

    None means the trials could not be fetched, which is distinct from `[]` --
    a version that genuinely has none. Conflating them lets a transient error
    silently drop trials from an otherwise successful collection.
    """
    resp = client.get(f"{api_url}/tasks/{task_id}/trials")
    if resp.status_code != 200:
        console.print(
            f"[red]Failed to fetch trials for {task_id} "
            f"(HTTP {resp.status_code}).[/red]"
        )
        return None
    data = resp.json()
    trials = data if isinstance(data, list) else data.get("trials", data.get("items", []))
    want = f"{task_id}-v{version}"
    out = []
    for t in trials:
        if (
            t.get("task_version_id") == want
            and not t.get("is_probe")
            and t.get("superseded_by_trial_id") is None
            and t.get("status") in _LINKABLE_STATUSES
        ):
            out.append(t["id"])
    return out


def _build_payload(*, name: str, tasks: list[str], trial_ids: list[str]) -> dict:
    return {"name": name, "task_ids": _dedupe(tasks), "trial_ids": _dedupe(trial_ids)}


def _share_url(api_url: str, public_token: str | None) -> str | None:
    if not public_token:
        return None
    return f"{get_dashboard_url(api_url)}/share/{public_token}"


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
    """
    tasks = tasks or []
    trial_ids = trial_ids or []
    if not _guard_sources(tasks=tasks, trial_ids=trial_ids):
        console.print("[red]Provide at least one --task or trial id.[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    coll_name = (name or "").strip() or "collection"

    public_url = None
    public_token = None
    publish_failed = False
    publish_status = None

    with httpx.Client(timeout=120.0, headers=get_auth_headers()) as client:
        # Version-pinned refs (name@N) are expanded client-side into explicit
        # trial ids so the deployed backend needs no change: `task_ids` still
        # means current-version, `trial_ids` carries the pinned versions.
        plain_tasks: list[str] = []
        pinned_trial_ids: list[str] = []
        for ref in _dedupe(tasks):
            base, version = _parse_task_ref(ref)
            if version is None:
                plain_tasks.append(base)
                continue
            task_id = _resolve_task_id(client, api_url, base)
            if not task_id:
                console.print(f"[red]Could not resolve task '{base}'.[/red]")
                raise typer.Exit(1)
            ids = _version_trial_ids(client, api_url, task_id, version)
            if ids is None:
                console.print(
                    f"[red]Could not read trials for {base}@{version}; aborting "
                    f"rather than silently dropping them.[/red]"
                )
                raise typer.Exit(1)
            if not ids:
                console.print(
                    f"[yellow]Warning:[/yellow] {base}@{version} has no linkable "
                    f"trials; it will be absent from the collection."
                )
            pinned_trial_ids.extend(ids)

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

        try:
            resp = client.post(f"{api_url}/experiments/collections", json=payload)
        except httpx.RequestError as e:
            console.print(f"[red]Failed to connect to API:[/red] {e}")
            raise typer.Exit(1)
        if resp.status_code != 200:
            console.print(
                f"[red]Collect failed:[/red] {resp.status_code} - {resp.text}"
            )
            raise typer.Exit(1)
        data = resp.json()

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

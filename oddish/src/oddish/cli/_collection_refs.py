from __future__ import annotations

import typer
from rich.console import Console

console = Console()


def _dedupe(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in raw if s and s.strip()))


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


def expand_task_refs(client, api_url: str, refs: list[str]) -> tuple[list[str], list[str]]:
    """Split task refs into plain names and client-expanded pinned trial ids.

    Version-pinned refs (``name@N``) are expanded client-side so the deployed
    backend needs no change: ``task_ids`` still means current-version, and
    ``trial_ids`` carries the pinned versions. Raises ``typer.Exit(1)`` rather
    than silently dropping trials when a pin cannot be read.
    """
    plain: list[str] = []
    pinned: list[str] = []
    for ref in _dedupe(refs):
        base, version = _parse_task_ref(ref)
        if version is None:
            plain.append(base)
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
        pinned.extend(ids)
    return plain, pinned

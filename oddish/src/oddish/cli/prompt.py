from __future__ import annotations

import difflib
import json as _json
from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()
prompt_app = typer.Typer(
    help="Manage versioned analyzer prompts.", no_args_is_help=True
)


def _resolve(api_url: str | None) -> str:
    url = api_url or get_api_url()
    require_api_key(url)
    return url


def _fail(resp: httpx.Response) -> None:
    console.print(f"[red]Failed ({resp.status_code}):[/red] {resp.text}")
    raise typer.Exit(1)


@prompt_app.command("list")
def list_prompts(
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """List all registered prompts."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}/prompts")
    if resp.status_code != 200:
        _fail(resp)
    for p in resp.json():
        console.print(f"{p['key']:32}  v{p.get('active_version')}  {p.get('description','')}")


@prompt_app.command("get")
def get_prompt(
    key: str,
    version: Annotated[Optional[int], typer.Option("--version", "-v")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Print a prompt's content (active version by default)."""
    url = _resolve(api_url)
    params = {"version": version} if version is not None else {}
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}/prompts/{key}", params=params)
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    if json_output:
        console.print_json(_json.dumps(data))
    else:
        console.print(data.get("content", ""))


@prompt_app.command("set")
def set_prompt(
    key: str,
    file: Annotated[Path, typer.Option("--file", "-f", help="File with prompt content.")],
    description: Annotated[Optional[str], typer.Option("--description", "-d")] = None,
    no_activate: Annotated[bool, typer.Option("--no-activate", help="Append without activating.")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Append a new prompt version from a file (activates it by default)."""
    url = _resolve(api_url)
    content = file.read_text()
    payload: dict = {"content": content, "activate": not no_activate}
    if description is not None:
        payload["description"] = description
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.put(f"{url}/prompts/{key}", json=payload)
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    console.print(
        f"[green]Set {key}[/green] active_version={data.get('active_version')}"
    )


@prompt_app.command("versions")
def versions(
    key: str,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """List a prompt's versions."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}/prompts/{key}/versions")
    if resp.status_code != 200:
        _fail(resp)
    for v in resp.json():
        console.print(f"v{v['version']:<4} {v.get('created_at','')}  {v.get('created_by') or ''}")


@prompt_app.command("activate")
def activate(
    key: str,
    version: int,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Point the active version at an existing version number."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.post(f"{url}/prompts/{key}/activate", json={"version": version})
    if resp.status_code != 200:
        _fail(resp)
    console.print(f"[green]Activated {key} v{version}[/green]")


@prompt_app.command("seed")
def seed(
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Create any missing built-in prompts from their seed content."""
    from oddish.core.prompt_seeds import PROMPT_SEEDS

    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        for key, (description, content) in PROMPT_SEEDS.items():
            got = client.get(f"{url}/prompts/{key}")
            if got.status_code == 200:
                console.print(f"[dim]{key}: exists, skipping[/dim]")
                continue
            resp = client.put(
                f"{url}/prompts/{key}",
                json={"content": content, "description": description, "activate": True},
            )
            if resp.status_code != 200:
                _fail(resp)
            console.print(f"[green]Seeded {key}[/green]")


@prompt_app.command("diff")
def diff(
    key: str,
    version_a: int,
    version_b: int,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Unified diff between two versions of a prompt."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        ra = client.get(f"{url}/prompts/{key}", params={"version": version_a})
        rb = client.get(f"{url}/prompts/{key}", params={"version": version_b})
    for r in (ra, rb):
        if r.status_code != 200:
            _fail(r)
    a = ra.json().get("content", "").splitlines(keepends=True)
    b = rb.json().get("content", "").splitlines(keepends=True)
    for line in difflib.unified_diff(a, b, fromfile=f"{key}@v{version_a}", tofile=f"{key}@v{version_b}"):
        console.print(line.rstrip("\n"))

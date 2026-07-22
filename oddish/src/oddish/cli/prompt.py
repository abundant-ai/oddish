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
    help="Manage versioned analyzer prompts (latest version is always live).",
    no_args_is_help=True,
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
        console.print(f"{p['kind']:32}  v{p.get('latest_version')}  {p.get('description','')}")


@prompt_app.command("get")
def get_prompt(
    kind: str,
    version: Annotated[Optional[int], typer.Option("--version", "-v")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Print a prompt's content (active version by default)."""
    url = _resolve(api_url)
    params = {"version": version} if version is not None else {}
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}/prompts/{kind}", params=params)
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    if json_output:
        console.print_json(_json.dumps(data))
    else:
        console.print(data.get("content", ""))


@prompt_app.command("set")
def set_prompt(
    kind: str,
    file: Annotated[Path, typer.Option("--file", "-f", help="File with prompt content.")],
    description: Annotated[Optional[str], typer.Option("--description", "-d")] = None,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Append a new prompt version from a file (it becomes live immediately — latest always runs)."""
    url = _resolve(api_url)
    content = file.read_text()
    payload: dict = {"content": content}
    if description is not None:
        payload["description"] = description
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.put(f"{url}/prompts/{kind}", json=payload)
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    console.print(f"[green]Set {kind}[/green] latest_version={data.get('latest_version')}")


@prompt_app.command("versions")
def versions(
    kind: str,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """List a prompt's versions."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}/prompts/{kind}/versions")
    if resp.status_code != 200:
        _fail(resp)
    for v in resp.json():
        console.print(f"v{v['version']:<4} {v.get('created_at','')}  {v.get('created_by') or ''}")


@prompt_app.command("seed")
def seed(
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Create any missing built-in prompts from their seed content."""
    from oddish.core.prompt_seeds import PROMPT_SEEDS

    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        for kind, (description, content) in PROMPT_SEEDS.items():
            got = client.get(f"{url}/prompts/{kind}")
            if got.status_code == 200:
                console.print(f"[dim]{kind}: exists, skipping[/dim]")
                continue
            resp = client.put(
                f"{url}/prompts/{kind}",
                json={"content": content, "description": description},
            )
            if resp.status_code != 200:
                _fail(resp)
            console.print(f"[green]Seeded {kind}[/green]")


@prompt_app.command("diff")
def diff(
    kind: str,
    version_a: int,
    version_b: int,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Unified diff between two versions of a prompt."""
    url = _resolve(api_url)
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        ra = client.get(f"{url}/prompts/{kind}", params={"version": version_a})
        rb = client.get(f"{url}/prompts/{kind}", params={"version": version_b})
    for r in (ra, rb):
        if r.status_code != 200:
            _fail(r)
    a = ra.json().get("content", "").splitlines(keepends=True)
    b = rb.json().get("content", "").splitlines(keepends=True)
    for line in difflib.unified_diff(a, b, fromfile=f"{kind}@v{version_a}", tofile=f"{kind}@v{version_b}"):
        console.print(line.rstrip("\n"))

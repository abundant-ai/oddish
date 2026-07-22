from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import get_api_url, get_auth_headers

console = Console()
prompt_app = typer.Typer(help="Manage versioned analyzer prompts.", no_args_is_help=True)


def _url(value: str | None) -> str:
    return (value or get_api_url()).rstrip("/")


def _request(method: str, url: str, **kwargs) -> dict | list:
    with httpx.Client(timeout=30, headers=get_auth_headers(url)) as client:
        response = client.request(method, url, **kwargs)
    if response.status_code != 200:
        console.print(f"[red]Failed ({response.status_code}):[/red] {response.text}")
        raise typer.Exit(1)
    return response.json()


@prompt_app.command("list")
def list_prompts(
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
) -> None:
    for prompt in _request("GET", f"{_url(api_url)}/prompts"):
        console.print(
            f"{prompt['key']:32} v{prompt.get('active_version')}  {prompt.get('description', '')}"
        )


@prompt_app.command("get")
def get_prompt(
    key: str,
    version: Annotated[Optional[int], typer.Option("--version", "-v")] = None,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
) -> None:
    params = {"version": version} if version is not None else None
    data = _request("GET", f"{_url(api_url)}/prompts/{key}", params=params)
    console.print(data.get("content", ""))


@prompt_app.command("set")
def set_prompt(
    key: str,
    file: Annotated[Path, typer.Option("--file", "-f")],
    description: Annotated[Optional[str], typer.Option("--description", "-d")] = None,
    no_activate: Annotated[bool, typer.Option("--no-activate")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
) -> None:
    payload = {
        "content": file.read_text(),
        "description": description,
        "activate": not no_activate,
    }
    data = _request("PUT", f"{_url(api_url)}/prompts/{key}", json=payload)
    console.print(f"[green]Set {key}[/green] active_version={data['active_version']}")


@prompt_app.command("versions")
def versions(
    key: str,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
) -> None:
    for item in _request("GET", f"{_url(api_url)}/prompts/{key}/versions"):
        console.print(f"v{item['version']:<4} {item['created_at']}  {item.get('created_by') or ''}")


@prompt_app.command("activate")
def activate(
    key: str,
    version: int,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
) -> None:
    _request(
        "POST", f"{_url(api_url)}/prompts/{key}/activate", json={"version": version}
    )
    console.print(f"[green]Activated {key} v{version}[/green]")

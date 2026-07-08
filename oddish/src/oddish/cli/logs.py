from __future__ import annotations

import time
from typing import Annotated, Any, Callable

import httpx
import typer
from rich.console import Console
from rich.markup import escape

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()

POLL_INTERVAL_SEC = 2.0

class _TransientError(Exception):
    pass


def _render_event(event: dict) -> str | None:
    kind = event.get("kind")
    payload = event.get("payload") or {}
    tail = " [dim]…[/dim]" if payload.get("truncated") else ""
    if kind == "message":
        text = str(payload.get("text") or "").strip()
        return f"{escape(text)}{tail}" if text else None
    if kind == "tool_use":
        name = escape(str(payload.get("name") or "tool"))
        arg = escape(str(payload.get("input") or "").strip().replace("\n", " ")[:160])
        return f"[cyan]⚙ {name}[/cyan] [dim]{arg}[/dim]{tail}"
    if kind == "tool_result":
        text = str(payload.get("content") or "").strip().replace("\n", " ")[:160]
        if not text:
            return None
        style = "red" if payload.get("is_error") else "dim"
        return f"[{style}]  ↳ {escape(text)}[/{style}]{tail}"
    if kind == "summary":
        text = str(payload.get("text") or "").strip()
        return f"[green]{escape(text)}[/green]{tail}" if text else None
    return None


def stream_logs(
    fetch: Callable[[int | None, int], dict],
    *,
    follow: bool,
    emit: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    attempt: int | None = None
    after_seq = 0
    last_cost: Any = None
    seen = 0
    while True:
        try:
            data = fetch(attempt, after_seq)
        except (_TransientError, httpx.HTTPError) as exc:
            if follow:
                sleep(POLL_INTERVAL_SEC)
                continue
            if isinstance(exc, _TransientError):
                console.print(f"[red]Failed to fetch live logs:[/red] {exc}")
                raise typer.Exit(1) from exc
            raise
        resp_attempt = data["attempt"]
        if attempt is not None and resp_attempt != attempt:
            emit("[dim]— trial retried; restarting transcript —[/dim]")
            after_seq = 0
        attempt = resp_attempt
        events = data["events"]
        seen += len(events)
        for event in events:
            line = _render_event(event)
            if line is not None:
                emit(line)
        if not events:
            if not follow or data.get("done"):
                return seen
            sleep(POLL_INTERVAL_SEC)
            continue
        after_seq = data["next_seq"]
        usage = data["usage"]
        cost = usage.get("cost_usd")
        if cost is not None and cost != last_cost:
            tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
            emit(f"[dim]${cost:.4f} · {tokens:,} tokens[/dim]")
            last_cost = cost


def _fetch(
    client: httpx.Client, api_url: str, trial_id: str
) -> Callable[[int | None, int], dict]:
    def fetch(attempt: int | None, after_seq: int) -> dict:
        params: dict[str, Any] = {"after_seq": after_seq}
        if attempt is not None:
            params["attempt"] = attempt
        response = client.get(f"{api_url}/trials/{trial_id}/live", params=params)
        if response.status_code == 404:
            console.print(f"[red]Trial {trial_id} not found[/red]")
            raise typer.Exit(1)
        if response.status_code != 200:
            if response.status_code in (500, 502, 503, 504):
                raise _TransientError(str(response.status_code))
            console.print(f"[red]Failed to fetch live logs:[/red] {response.text}")
            raise typer.Exit(1)
        return response.json()

    return fetch


def logs(
    trial_id: Annotated[
        str,
        typer.Argument(help="Trial ID to stream live transcript + cost for"),
    ],
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Poll for new events until the trial ends"),
    ] = False,
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
) -> None:
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    try:
        with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
            seen = stream_logs(
                _fetch(client, api_url, trial_id),
                follow=follow,
                emit=console.print,
            )
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped[/dim]")
        raise typer.Exit(0)
    except httpx.HTTPError as exc:
        console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc

    if seen == 0:
        console.print(f"[dim]No live events for trial {trial_id}[/dim]")

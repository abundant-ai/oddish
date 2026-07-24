"""Read a trial's trajectory in parts rather than whole.

Exists for analyzer agents running in a sandbox: a full ATIF trajectory is tens
of thousands of tokens, so pulling it wholesale burns most of a context window
before any analysis happens. These commands expose the segmentation the
trajectory-summary block already produces -- a prose summary, star steps, and a
labelled component index -- so an agent can read the map first and then fetch
only the steps it needs.

Step filtering is client-side on purpose: the token cost that matters is what
reaches the agent's context, which is this command's stdout, not what the CLI
downloads over the wire.
"""

from __future__ import annotations

import json as _json
from typing import Annotated, Any, Optional

import httpx
import typer
from rich.console import Console
from rich.markup import escape

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key

console = Console()
trajectory_app = typer.Typer(
    help="Inspect a trial's trajectory in parts: summary, components, selected steps.",
    no_args_is_help=True,
)


def _resolve(api_url: str | None) -> str:
    url = api_url or get_api_url()
    require_api_key(url)
    return url


def _fail(resp: httpx.Response) -> None:
    console.print(f"[red]Failed ({resp.status_code}):[/red] {resp.text}")
    raise typer.Exit(1)


def _get(url: str, path: str) -> Any:
    # Summary generation is an LLM call on a cache miss, so this needs a
    # tolerance well above the usual read timeout.
    with httpx.Client(timeout=300.0, headers=get_auth_headers()) as client:
        resp = client.get(f"{url}{path}")
    if resp.status_code != 200:
        _fail(resp)
    return resp.json()


def _fetch_summary(url: str, trial_id: str) -> dict:
    data = _get(url, f"/trials/{trial_id}/trajectory/summary")
    if not isinstance(data, dict):
        console.print("[red]Unexpected summary payload.[/red]")
        raise typer.Exit(1)
    return data


def _components(summary: dict) -> list[dict]:
    return [c for c in (summary.get("components") or []) if isinstance(c, dict)]


def _fmt_duration(ms: Any) -> str:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return "-"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _fmt_step_range(step_ids: list[int]) -> str:
    """Collapse a sorted id list into compact ranges (1-4,7,9-11)."""
    if not step_ids:
        return "-"
    ordered = sorted(step_ids)
    parts: list[str] = []
    start = previous = ordered[0]
    for current in ordered[1:]:
        if current == previous + 1:
            previous = current
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = current
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


def _parse_step_selector(raw: str) -> list[int]:
    """Parse ``3,5,8-12`` into a sorted, de-duplicated id list."""
    out: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk.lstrip("-"):
            lo_raw, _, hi_raw = chunk.partition("-")
            try:
                lo, hi = int(lo_raw), int(hi_raw)
            except ValueError:
                console.print(f"[red]Bad step range:[/red] {chunk}")
                raise typer.Exit(1)
            if lo > hi:
                lo, hi = hi, lo
            out.update(range(lo, hi + 1))
        else:
            try:
                out.add(int(chunk))
            except ValueError:
                console.print(f"[red]Bad step id:[/red] {chunk}")
                raise typer.Exit(1)
    return sorted(out)


@trajectory_app.command("summary")
def summary(
    trial_id: Annotated[str, typer.Argument(help="Trial id.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Print the trajectory summary and its star steps.

    Read this before anything else: it is the cheapest description of what the
    agent did, and its star steps point at the moments that decided the outcome.
    """
    url = _resolve(api_url)
    data = _fetch_summary(url, trial_id)
    if json_output:
        console.print_json(_json.dumps(data))
        return

    console.print(f"[bold]Summary[/bold] (trial {trial_id})")
    summary_text = data.get("summary")
    if summary_text:
        console.print(str(summary_text), markup=False)
    else:
        console.print("[dim]none[/dim]")

    highlights = [h for h in (data.get("highlights") or []) if isinstance(h, dict)]
    if highlights:
        console.print("\n[bold]Star steps[/bold]")
        for h in highlights:
            console.print(
                f"  step {h.get('step_id')}: {h.get('title') or ''}", markup=False
            )
            why = (h.get("why") or "").strip()
            if why:
                console.print(f"    {escape(why)}", style="dim")

    count = len(_components(data))
    if count:
        console.print(
            f"\n[dim]{count} components. "
            f"`oddish trajectory components {trial_id}` for the index.[/dim]"
        )


@trajectory_app.command("components")
def components(
    trial_id: Annotated[str, typer.Argument(help="Trial id.")],
    label: Annotated[
        Optional[str],
        typer.Option("--label", "-l", help="Only components with this taxonomy label."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Print the labelled component index.

    Each row is a contiguous phase of work with its taxonomy label, step range,
    tool-call count and wall-clock duration -- enough to choose which steps are
    worth reading in full without reading any of them.
    """
    url = _resolve(api_url)
    data = _fetch_summary(url, trial_id)
    rows = list(enumerate(_components(data)))
    if label:
        rows = [
            (index, component)
            for index, component in rows
            if component.get("trajectory_component") == label
        ]

    if json_output:
        # Carry the original component index so `--label --json` callers can feed
        # it back to `steps --component N` -- array position no longer matches
        # after filtering.
        console.print_json(
            _json.dumps([{"index": index, **component} for index, component in rows])
        )
        return

    if not rows:
        console.print("[yellow]No components.[/yellow]")
        return

    console.print(f"[bold]{'#':<3} {'label':<26} {'steps':<14} {'n':>4} "
                  f"{'tools':>6} {'time':>8}[/bold]")
    for index, component in rows:
        step_ids = [s for s in (component.get("step_ids") or []) if isinstance(s, int)]
        console.print(
            f"{index:<3} {str(component.get('trajectory_component') or '?'):<26} "
            f"{_fmt_step_range(step_ids):<14} {len(step_ids):>4} "
            f"{int(component.get('tool_count') or 0):>6} "
            f"{_fmt_duration(component.get('duration_ms')):>8}",
            markup=False,
        )
        text = (component.get("summary") or "").strip()
        if text:
            console.print(f"    {escape(text)}", style="dim")

    console.print(
        f"\n[dim]Read one with: oddish trajectory steps {trial_id} --component N[/dim]"
    )


@trajectory_app.command("steps")
def steps(
    trial_id: Annotated[str, typer.Argument(help="Trial id.")],
    component: Annotated[
        Optional[list[int]],
        typer.Option("--component", "-c", help="Component index, repeatable."),
    ] = None,
    step_selector: Annotated[
        Optional[str],
        typer.Option("--steps", "-s", help="Step ids/ranges, e.g. 3,5,8-12."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
):
    """Print the full content of selected steps only.

    Select by component index (from `components`) or by explicit step ids.
    Supplying neither would print the entire trajectory, which defeats the
    purpose, so it is rejected.
    """
    url = _resolve(api_url)
    if not component and not step_selector:
        console.print(
            "[red]Select steps with --component or --steps.[/red] "
            "Printing every step defeats the point; use `components` to choose."
        )
        raise typer.Exit(1)

    wanted: set[int] = set(_parse_step_selector(step_selector) if step_selector else [])
    if component:
        rows = _components(_fetch_summary(url, trial_id))
        for index in component:
            if index < 0 or index >= len(rows):
                console.print(
                    f"[red]No component {index}[/red] "
                    f"({len(rows)} available, 0-{max(len(rows) - 1, 0)})."
                )
                raise typer.Exit(1)
            wanted.update(
                s for s in (rows[index].get("step_ids") or []) if isinstance(s, int)
            )

    trajectory = _get(url, f"/trials/{trial_id}/trajectory")
    if not isinstance(trajectory, dict):
        console.print("[yellow]No trajectory for this trial.[/yellow]")
        raise typer.Exit(1)

    selected = [
        step
        for step in (trajectory.get("steps") or [])
        if isinstance(step, dict) and step.get("step_id") in wanted
    ]
    if not selected:
        console.print("[yellow]No matching steps.[/yellow]")
        return

    if json_output:
        console.print_json(_json.dumps(selected))
        return

    for step in selected:
        console.print(f"\n[bold]── step {step.get('step_id')} ──[/bold]")
        console.print(_json.dumps(step, indent=2, default=str), markup=False)

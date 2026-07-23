from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import get_api_url, get_auth_headers, print_json

console = Console()


def _load_variants(specs: list[str]) -> list[dict[str, object]]:
    variants = []
    for spec in specs:
        kind, separator, raw_version = spec.strip().partition("@")
        if not kind:
            raise typer.BadParameter("--variant must be KIND or KIND@VERSION")
        item: dict[str, object] = {"kind": kind}
        if separator:
            try:
                version = int(raw_version)
            except ValueError as exc:
                raise typer.BadParameter("Prompt version must be an integer") from exc
            if version < 1:
                raise typer.BadParameter("Prompt version must be positive")
            item["version"] = version
        variants.append(item)
    return variants


def qa(
    scope_type: Annotated[str, typer.Argument(help="experiment, task, or trial")],
    scope_id: Annotated[str, typer.Argument(help="ID of the scoped object")],
    variants: Annotated[
        list[str],
        typer.Option("--variant", "-v", help="Saved prompt KIND or KIND@VERSION; repeatable."),
    ],
    model: Annotated[str, typer.Option("--model", "-m")] = "claude-sonnet-4-6",
    reasoning_effort: Annotated[Optional[str], typer.Option("--reasoning-effort")] = None,
    backend: Annotated[str, typer.Option("--backend", help="sandbox or api")] = "sandbox",
    allow_oddish_cli: Annotated[
        bool,
        typer.Option("--allow-oddish-cli", help="Forward this API key into the ephemeral sandbox."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    api_url: Annotated[Optional[str], typer.Option("--api-url", "-u")] = None,
) -> None:
    """Run one or more versioned QA prompts and compare their outputs."""
    if scope_type not in {"experiment", "task", "trial"}:
        raise typer.BadParameter("scope_type must be experiment, task, or trial")
    if reasoning_effort not in {None, "low", "medium", "high"}:
        raise typer.BadParameter("--reasoning-effort must be low, medium, or high")
    if backend not in {"sandbox", "api"}:
        raise typer.BadParameter("--backend must be sandbox or api")
    loaded = _load_variants(variants)
    if not loaded:
        raise typer.BadParameter("Provide at least one --variant KIND or KIND@VERSION")
    base = (api_url or get_api_url()).rstrip("/")
    payload = {
        "scope_type": scope_type, "scope_id": scope_id, "variants": loaded,
        "model": model, "reasoning_effort": reasoning_effort, "backend": backend,
        "allow_credential_forwarding": allow_oddish_cli,
    }
    with httpx.Client(timeout=1800, headers=get_auth_headers(base)) as client:
        response = client.post(f"{base}/qa/runs", json=payload)
    if response.status_code != 200:
        console.print(f"[red]QA run failed:[/red] {response.text}")
        raise typer.Exit(1)
    rows = response.json()
    if json_output:
        print_json(rows)
        return
    table = Table("Variant", "Version", "Run", "Block", "Status")
    for row in rows:
        table.add_row(row["prompt_kind"], f"v{row['prompt_version']}", row["id"], row["analyzer_block_id"], row["status"])
    console.print(table)
    for row in rows:
        console.rule(f"{row['prompt_kind']} v{row['prompt_version']}")
        console.print(row.get("output") or row.get("error") or "")

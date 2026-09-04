"""List curated model ids this Oddish process knows about."""

from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from oddish.config import list_curated_models

console = Console()


def models(
    agent: Annotated[
        Optional[str],
        typer.Option(
            "--agent",
            "-a",
            help="Hide providers locked away from this agent (e.g. grok-build).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print JSON instead of a table."),
    ] = False,
) -> None:
    """List curated Fireworks/DeepSeek model spellings.

    ``available`` reflects whether this process has the provider API key in
    its environment (useful for self-host). Hosted API containers may omit
    worker secrets — use the dashboard or ask an operator for deploy coverage.
    """
    rows = list_curated_models(agent=agent)
    if json_output:
        import json

        console.print_json(json.dumps(rows))
        return

    table = Table(title="Curated models", show_header=True)
    table.add_column("Canonical")
    table.add_column("Aliases")
    table.add_column("Credential")
    table.add_column("Key present")
    for row in rows:
        table.add_row(
            str(row["canonical"]),
            ", ".join(str(a) for a in row["aliases"]),  # type: ignore[arg-type]
            str(row["credential_env"]),
            "yes" if row["available"] else "no",
        )
    console.print(table)

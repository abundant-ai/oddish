from __future__ import annotations

import typer
from oddish.cli.clean import clean
from oddish.cli.pull import pull
from oddish.cli.run import run
from oddish.cli.status import status

app = typer.Typer(
    help="Oddish - Harbor eval scheduler with queues, retries, and monitoring.",
    no_args_is_help=True,
)

app.command()(run)
app.command()(status)
app.command()(clean)
app.command()(pull)


if __name__ == "__main__":
    app()

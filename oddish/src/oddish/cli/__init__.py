from __future__ import annotations

import typer
from oddish.cli.cancel import cancel
from oddish.cli.delete import delete
from oddish.cli.pull import pull
from oddish.cli.run import run
from oddish.cli.status import status
from oddish.cli.upload import upload

app = typer.Typer(
    help="Oddish - Harbor eval scheduler with queues, retries, and monitoring.",
    no_args_is_help=True,
)

app.command()(run)
app.command()(upload)
app.command()(status)
app.command()(cancel)
app.command()(delete)
app.command()(pull)


if __name__ == "__main__":
    app()

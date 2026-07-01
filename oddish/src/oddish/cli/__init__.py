from __future__ import annotations

import typer
from oddish.cli.backfill_analysis import backfill_analysis
from oddish.cli.cancel import cancel
from oddish.cli.combine import combine
from oddish.cli.delete import delete
from oddish.cli.experiment import experiment_app
from oddish.cli.ls import ls
from oddish.cli.publish import publish, unpublish
from oddish.cli.probe import probe_app
from oddish.cli.pull import pull
from oddish.cli.run import run
from oddish.cli.status import status
from oddish.cli.upload import upload

app = typer.Typer(
    help="Oddish - Harbor eval scheduler with queues, retries, and monitoring.",
    no_args_is_help=True,
)

app.command()(run)
app.add_typer(probe_app, name="probe")
app.command(name="backfill-analysis")(backfill_analysis)
app.command()(upload)
app.command(name="ls")(ls)
app.command()(status)
app.command()(cancel)
app.command()(combine)
app.command()(delete)
app.add_typer(experiment_app, name="experiment")
app.command()(pull)
app.command()(publish)
app.command()(unpublish)


if __name__ == "__main__":
    app()

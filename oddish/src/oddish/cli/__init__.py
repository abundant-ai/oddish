from __future__ import annotations

import typer
from oddish.cli.admin import admin_app
from oddish.cli.assign import assign
from oddish.cli.backfill_analysis import backfill_analysis
from oddish.cli.cancel import cancel
from oddish.cli.collect import collect
from oddish.cli.combine import combine
from oddish.cli.cost_exclusions import cost_exclusions_app
from oddish.cli.costs import costs
from oddish.cli.delete import delete
from oddish.cli.delivery import delivery_app
from oddish.cli.experiment import experiment_app
from oddish.cli.link import link_app
from oddish.cli.logs import logs
from oddish.cli.ls import ls
from oddish.cli.publish import publish, unpublish
from oddish.cli.probe import probe_app
from oddish.cli.pull import pull
from oddish.cli.preflight import preflight
from oddish.cli.run import run
from oddish.cli.skill import skill
from oddish.cli.status import status
from oddish.cli.upload import upload

app = typer.Typer(
    help="Oddish - Harbor eval scheduler with queues, retries, and monitoring.",
    no_args_is_help=True,
)

app.command()(run)
app.command()(assign)
app.add_typer(probe_app, name="probe")
app.command(name="backfill-analysis")(backfill_analysis)
app.command()(upload)
app.command()(preflight)
app.command(name="ls")(ls)
app.command()(status)
app.command()(skill)
app.command(help="Stream a running trial's live transcript and running cost.")(logs)
app.command()(cancel)
app.command()(combine)
app.command()(costs)
app.add_typer(cost_exclusions_app, name="cost-exclusions")
app.command()(collect)
app.command()(delete)
app.add_typer(admin_app, name="admin")
app.add_typer(delivery_app, name="delivery")
app.add_typer(experiment_app, name="experiment")
app.add_typer(link_app, name="link")
app.command()(pull)
app.command()(publish)
app.command()(unpublish)


if __name__ == "__main__":
    app()

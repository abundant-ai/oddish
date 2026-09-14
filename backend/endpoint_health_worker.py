"""Register background monitoring separately from task dispatch and Slack delivery."""

import modal
from sqlalchemy import text

from endpoint_health import run_checks
from modal_app import app, image, slack_notification_secrets
from oddish.db import close_database_connections, get_session


@app.function(
    image=image,
    secrets=slack_notification_secrets,
    timeout=120,
    max_containers=1,
    schedule=modal.Cron("* * * * *"),
)
async def check_model_endpoints() -> None:
    try:
        await run_checks()
    finally:
        await close_database_connections()


@app.function(
    image=image,
    secrets=slack_notification_secrets,
    timeout=120,
    max_containers=1,
    schedule=modal.Cron("17 * * * *"),
)
async def prune_endpoint_checks() -> None:
    try:
        # Limit each transaction; catch up gradually after missed maintenance.
        async with get_session() as session:
            await session.execute(
                text("""
                DELETE FROM endpoint_checks WHERE (monitor_id, claim_token) IN (
                    SELECT monitor_id, claim_token FROM endpoint_checks
                    WHERE checked_at < now() - interval '30 days'
                    ORDER BY checked_at LIMIT 20000
                )
            """)
            )
    finally:
        await close_database_connections()

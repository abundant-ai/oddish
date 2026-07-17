"""Thresholds and escalation list for the Slack cost alerts.

The ``AlertSettings`` field defaults are the deploy-time defaults. A single
``slack_alert_settings`` row overrides them so an admin can retune alerting from
the cost dashboard without a redeploy; no row -- or no table yet, mid-migration
-- leaves the defaults standing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from models import SlackAlertSettingsModel
from oddish.db import get_session, utcnow
from pg_errors import is_undefined_table_error

log = logging.getLogger("oddish.slack_alert_settings")

SETTINGS_ROW_ID = "global"

# Everyone here is @-mentioned in the channel when a single trial clears the
# escalation threshold. Matched to Slack accounts by email, so these must be the
# addresses on their Slack profiles, not necessarily their Clerk logins.
DEFAULT_ALWAYS_PING_EMAILS: tuple[str, ...] = (
    "charles@abundant.ai",
    "ke@abundant.ai",
    "meji@abundant.ai",
    "jesse@abundant.ai",
)


@dataclass(frozen=True)
class AlertSettings:
    experiment_milestone_usd: float = 1000.0
    experiment_repeat_usd: float = 1000.0
    trial_ping_usd: float = 200.0
    trial_escalation_usd: float = 1000.0
    always_ping_emails: tuple[str, ...] = DEFAULT_ALWAYS_PING_EMAILS
    is_override: bool = False


DEFAULT_ALERT_SETTINGS = AlertSettings()


async def get_alert_settings(session: AsyncSession) -> AlertSettings:
    """The override row, or the defaults when no admin has set one.

    Raises ``ProgrammingError`` if the table does not exist yet; callers that
    must survive that use ``read_alert_settings``.
    """
    row = (
        await session.execute(
            select(SlackAlertSettingsModel).where(
                SlackAlertSettingsModel.id == SETTINGS_ROW_ID
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return DEFAULT_ALERT_SETTINGS
    return AlertSettings(
        experiment_milestone_usd=float(row.experiment_milestone_usd),
        experiment_repeat_usd=float(row.experiment_repeat_usd),
        trial_ping_usd=float(row.trial_ping_usd),
        trial_escalation_usd=float(row.trial_escalation_usd),
        always_ping_emails=tuple(row.always_ping_emails),
        is_override=True,
    )


async def read_alert_settings() -> AlertSettings:
    """Same, but never raises, and takes a session of its own.

    A missing table poisons the transaction it is queried in, so this cannot
    share the alert run's session -- the reads that follow would all fail on the
    aborted transaction rather than on anything of their own.
    """
    try:
        async with get_session() as session:
            return await get_alert_settings(session)
    except ProgrammingError as exc:
        if not is_undefined_table_error(exc):
            raise
        log.warning("slack_alert_settings missing; alerting on default thresholds")
        return DEFAULT_ALERT_SETTINGS


async def set_alert_settings(
    session: AsyncSession,
    *,
    experiment_milestone_usd: float,
    experiment_repeat_usd: float,
    trial_ping_usd: float,
    trial_escalation_usd: float,
    always_ping_emails: list[str],
    updated_by_user_id: str | None,
) -> AlertSettings:
    values = {
        "experiment_milestone_usd": Decimal(str(experiment_milestone_usd)),
        "experiment_repeat_usd": Decimal(str(experiment_repeat_usd)),
        "trial_ping_usd": Decimal(str(trial_ping_usd)),
        "trial_escalation_usd": Decimal(str(trial_escalation_usd)),
        "always_ping_emails": always_ping_emails,
        "updated_at": utcnow(),
        "updated_by_user_id": updated_by_user_id,
    }
    await session.execute(
        pg_insert(SlackAlertSettingsModel)
        .values(id=SETTINGS_ROW_ID, **values)
        .on_conflict_do_update(index_elements=["id"], set_=values)
    )
    return await get_alert_settings(session)


async def clear_alert_settings(session: AsyncSession) -> AlertSettings:
    """Drop the override so the deploy-time defaults take over again."""
    await session.execute(
        SlackAlertSettingsModel.__table__.delete().where(
            SlackAlertSettingsModel.id == SETTINGS_ROW_ID
        )
    )
    return DEFAULT_ALERT_SETTINGS

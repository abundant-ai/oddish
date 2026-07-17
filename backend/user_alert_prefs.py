"""Per-user overrides for which Slack DM alerts a person receives, and at what
cutoffs.

Each user gets a row in ``user_alert_preferences`` only once they change
something; no row means every default below stands (all five DM types on, both
cost cutoffs inherited from the global ``slack_alert_settings``). The cutoffs
are nullable precisely so "inherit the admin default" is distinct from "set it
to this number" -- a null rides whatever the admin last set, a value pins it for
this person regardless.

Only a person's own DMs are affected. The in-channel escalation on a very
expensive trial is shared oversight and ignores these entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from models import UserAlertPreferencesModel, UserModel
from oddish.db import get_session, utcnow
from pg_errors import is_undefined_column_or_table_error

log = logging.getLogger("oddish.user_alert_prefs")


@dataclass(frozen=True)
class UserAlertPrefs:
    cost_milestone_enabled: bool = True
    expensive_trial_enabled: bool = True
    experiment_failed_enabled: bool = True
    trial_failed_enabled: bool = True
    qa_failed_enabled: bool = True
    # None means "inherit the admin/global cutoff"; a value pins it for this
    # person. The milestone value overrides both the first threshold and the
    # repeat interval -- a user thinks in one "DM me every $X", not two.
    experiment_milestone_usd: float | None = None
    trial_ping_usd: float | None = None


DEFAULT_USER_ALERT_PREFS = UserAlertPrefs()


def _from_row(row: UserAlertPreferencesModel) -> UserAlertPrefs:
    def _usd(value: Decimal | None) -> float | None:
        return None if value is None else float(value)

    return UserAlertPrefs(
        cost_milestone_enabled=row.cost_milestone_enabled,
        expensive_trial_enabled=row.expensive_trial_enabled,
        experiment_failed_enabled=row.experiment_failed_enabled,
        trial_failed_enabled=row.trial_failed_enabled,
        qa_failed_enabled=row.qa_failed_enabled,
        experiment_milestone_usd=_usd(row.experiment_milestone_usd),
        trial_ping_usd=_usd(row.trial_ping_usd),
    )


async def get_user_alert_prefs(session: AsyncSession, user_id: str) -> UserAlertPrefs:
    row = (
        await session.execute(
            select(UserAlertPreferencesModel).where(
                UserAlertPreferencesModel.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return DEFAULT_USER_ALERT_PREFS if row is None else _from_row(row)


async def set_user_alert_prefs(
    session: AsyncSession,
    user_id: str,
    prefs: UserAlertPrefs,
) -> UserAlertPrefs:
    def _usd(value: float | None) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    values = {
        "cost_milestone_enabled": prefs.cost_milestone_enabled,
        "expensive_trial_enabled": prefs.expensive_trial_enabled,
        "experiment_failed_enabled": prefs.experiment_failed_enabled,
        "trial_failed_enabled": prefs.trial_failed_enabled,
        "qa_failed_enabled": prefs.qa_failed_enabled,
        "experiment_milestone_usd": _usd(prefs.experiment_milestone_usd),
        "trial_ping_usd": _usd(prefs.trial_ping_usd),
        "updated_at": utcnow(),
    }
    await session.execute(
        pg_insert(UserAlertPreferencesModel)
        .values(user_id=user_id, **values)
        .on_conflict_do_update(index_elements=["user_id"], set_=values)
    )
    return await get_user_alert_prefs(session, user_id)


async def read_prefs_by_email() -> dict[str, UserAlertPrefs]:
    """Every override row, keyed by the owner's lowercased email, for one alert
    run. Emails with no row are simply absent -- callers fall back to
    ``DEFAULT_USER_ALERT_PREFS``. Never raises: a missing table or column
    (deploy-before-migrate) fails open to "no overrides", so alerting keeps its
    pre-feature behaviour rather than dropping DMs.
    """
    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(UserModel.email, UserAlertPreferencesModel).join(
                        UserAlertPreferencesModel,
                        UserAlertPreferencesModel.user_id == UserModel.id,
                    )
                )
            ).all()
    except ProgrammingError as exc:
        if not is_undefined_column_or_table_error(exc):
            raise
        log.warning("user_alert_preferences unavailable; alerting with no overrides")
        return {}
    return {
        email.strip().lower(): _from_row(row)
        for email, row in rows
        if email and email.strip()
    }

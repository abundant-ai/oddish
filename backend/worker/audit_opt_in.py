"""Org-level pre-trial audit opt-in, read from organizations.settings."""

from __future__ import annotations

from sqlalchemy import select

from models import OrganizationModel
from oddish.config import settings
from oddish.db import TaskModel, get_session

_SETTING = "pre_trial_analysis_enabled"


async def org_audit_enabled(task_id: str) -> bool:
    async with get_session() as session:
        org_id = await session.scalar(
            select(TaskModel.org_id).where(TaskModel.id == task_id)
        )
        if org_id is None:
            return settings.pre_trial_enabled
        org = await session.get(OrganizationModel, org_id)
    value = ((org.settings if org else None) or {}).get(_SETTING)
    if isinstance(value, bool):
        return value
    return settings.pre_trial_enabled

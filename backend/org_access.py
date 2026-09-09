"""Hosted execution approval, read from the database rather than auth caches."""

from fastapi import HTTPException
from sqlalchemy import select

from models import OrganizationModel
from oddish.db import get_read_session


async def require_execution_org(org_id: str | None) -> None:
    if org_id is not None:
        async with get_read_session() as session:
            approved = await session.scalar(
                select(OrganizationModel.id).where(
                    OrganizationModel.id == org_id,
                    OrganizationModel.is_active.is_(True),
                    OrganizationModel.execution_enabled.is_(True),
                )
            )
        if approved is not None:
            return
    raise HTTPException(
        status_code=403,
        detail="This organization needs Abundant approval. Select an approved organization or ask Abundant for access.",
    )

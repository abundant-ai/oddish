"""Authorize hosted task file reads and select their revision in one statement."""

from fastapi import HTTPException, Request
from sqlalchemy import select, true

from auth import authorize_bound_analysis_request
from auth.types import AuthContext
from models import APIKeyScope, OrganizationModel
from oddish.core.task_files import (
    TaskFileSource,
    task_file_source_from_row,
    task_file_source_query,
)
from oddish.db import get_read_session


async def resolve_authorized_task_file_source(
    request: Request, auth: AuthContext, *, task_id: str, version: int | None
) -> TaskFileSource:
    if not auth.is_authenticated:
        raise HTTPException(
            401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
        )
    auth.require_scope(APIKeyScope.READ)
    source = task_file_source_query(task_id, version, auth.org_id).subquery()
    # Start from the approved organization so a missing task remains 404 while
    # a missing/inactive/unapproved organization remains 403. No task row from
    # another organization can enter the outer join.
    query = (
        select(OrganizationModel.id.label("approved_org_id"), *source.c)
        .select_from(OrganizationModel)
        .outerjoin(source, true())
        .where(
            OrganizationModel.id == auth.org_id,
            OrganizationModel.is_active.is_(True),
            OrganizationModel.execution_enabled.is_(True),
        )
    )
    async with get_read_session() as session:
        # Bound analysis credentials keep their additional resource restrictions.
        await authorize_bound_analysis_request(request, auth, session)
        row = (await session.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(
            403,
            "This organization needs Abundant approval. Select an approved organization or ask Abundant for access.",
        )
    if row.task_id is None:
        raise HTTPException(404, f"Task {task_id} not found")
    return task_file_source_from_row(row)

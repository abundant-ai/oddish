"""Administrative assignment of QA review work across task IDs."""

from typing import Annotated

from auth import AuthContext, require_admin
from fastapi import APIRouter, Depends, HTTPException
from models import UserModel
from oddish.core.qa_work import assign_task_qa_work_core
from oddish.db import get_session
from oddish.schemas import QAWorkAssign, QAWorkAssignResponse
from sqlalchemy import func, or_, select

router = APIRouter()


@router.post("/tasks/qa-work/assign", response_model=QAWorkAssignResponse)
async def assign_qa_work(
    data: QAWorkAssign,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QAWorkAssignResponse:
    assignee = data.assignee.strip()
    if not assignee:
        raise HTTPException(status_code=400, detail="Assignee must not be empty")
    async with get_session() as session:
        users = (
            await session.scalars(
                select(UserModel).where(
                    UserModel.org_id == auth.org_id,
                    or_(
                        UserModel.id == assignee,
                        func.lower(UserModel.email) == assignee.lower(),
                        func.lower(UserModel.github_username)
                        == assignee.removeprefix("@").lower(),
                    ),
                )
            )
        ).all()
        owner = next((user for user in users if user.id == assignee), None)
        if owner is None:
            if not users:
                raise HTTPException(
                    status_code=404, detail="Assignee not found in this organization"
                )
            if len(users) != 1:
                raise HTTPException(
                    status_code=409, detail="Assignee is ambiguous; use their user ID"
                )
            owner = users[0]
        result = await assign_task_qa_work_core(
            session,
            org_id=auth.org_id,
            task_ids=data.task_ids,
            owner_user_id=owner.id,
            replace=data.replace,
        )
        await session.commit()
        return result

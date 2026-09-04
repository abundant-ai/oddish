"""Task-ID QA assignment for standalone installations."""

from fastapi import APIRouter, HTTPException

from oddish.core.qa_work import assign_task_qa_work_core
from oddish.db import get_session
from oddish.schemas import QAWorkAssign, QAWorkAssignResponse

router = APIRouter()


@router.post("/tasks/qa-work/assign", response_model=QAWorkAssignResponse)
async def assign_qa_work(data: QAWorkAssign) -> QAWorkAssignResponse:
    owner_user_id = data.assignee.strip()
    if not owner_user_id:
        raise HTTPException(status_code=400, detail="Assignee must not be empty")
    async with get_session() as session:
        result = await assign_task_qa_work_core(
            session,
            org_id=None,
            task_ids=data.task_ids,
            owner_user_id=owner_user_id,
            replace=data.replace,
        )
        await session.commit()
        return result

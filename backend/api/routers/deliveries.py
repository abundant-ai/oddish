"""Authenticated delivery-checklist endpoints (docs/delivery-design.md).

Reads need the ordinary TASKS scope; every mutation is admin-only. All
readiness state is computed in ``oddish.core.deliveries`` — these routes
only add auth and transaction boundaries.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import APIKeyScope, AuthContext, require_admin, require_auth
from oddish.core.deliveries import (
    add_delivery_tasks_core,
    create_delivery_core,
    delete_delivery_core,
    finalize_delivery_core,
    get_delivery_board_core,
    get_task_qa_history_core,
    list_deliveries_core,
    patch_delivery_core,
    patch_delivery_task_core,
    remove_delivery_task_core,
    set_manual_check_core,
)
from oddish.db import get_session
from oddish.schemas import (
    DeliveryBoardResponse,
    DeliveryCreate,
    DeliveryListItem,
    DeliveryPatch,
    DeliveryResponse,
    DeliveryTaskPatch,
    DeliveryTasksAdd,
    ManualCheckSet,
    TaskQAHistoryResponse,
)

router = APIRouter()


@router.post("/deliveries", response_model=DeliveryResponse)
async def create_delivery(
    data: DeliveryCreate,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> DeliveryResponse:
    async with get_session() as session:
        delivery = await create_delivery_core(
            session, data=data, org_id=auth.org_id, user_id=auth.user_id
        )
        await session.commit()
        return DeliveryResponse.model_validate(delivery)


@router.get("/deliveries", response_model=list[DeliveryListItem])
async def list_deliveries(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[DeliveryListItem]:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        return await list_deliveries_core(session, org_id=auth.org_id)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryBoardResponse)
async def get_delivery_board(
    delivery_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> DeliveryBoardResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        return await get_delivery_board_core(
            session, delivery_id=delivery_id, org_id=auth.org_id
        )


@router.patch("/deliveries/{delivery_id}", response_model=DeliveryResponse)
async def patch_delivery(
    delivery_id: str,
    data: DeliveryPatch,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> DeliveryResponse:
    async with get_session() as session:
        delivery = await patch_delivery_core(
            session, delivery_id=delivery_id, org_id=auth.org_id, data=data
        )
        await session.commit()
        return DeliveryResponse.model_validate(delivery)


@router.delete("/deliveries/{delivery_id}")
async def delete_delivery(
    delivery_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    async with get_session() as session:
        await delete_delivery_core(
            session, delivery_id=delivery_id, org_id=auth.org_id
        )
        await session.commit()
        return {"deleted": delivery_id}


@router.post("/deliveries/{delivery_id}/tasks")
async def add_delivery_tasks(
    delivery_id: str,
    data: DeliveryTasksAdd,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    async with get_session() as session:
        added = await add_delivery_tasks_core(
            session, delivery_id=delivery_id, org_id=auth.org_id, data=data
        )
        await session.commit()
        return {"added": added}


@router.delete("/deliveries/{delivery_id}/tasks/{task_id}")
async def remove_delivery_task(
    delivery_id: str,
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    async with get_session() as session:
        await remove_delivery_task_core(
            session, delivery_id=delivery_id, org_id=auth.org_id, task_id=task_id
        )
        await session.commit()
        return {"removed": task_id}


@router.patch("/deliveries/{delivery_id}/tasks/{task_id}")
async def patch_delivery_task(
    delivery_id: str,
    task_id: str,
    data: DeliveryTaskPatch,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    async with get_session() as session:
        await patch_delivery_task_core(
            session,
            delivery_id=delivery_id,
            org_id=auth.org_id,
            task_id=task_id,
            data=data,
        )
        await session.commit()
        return {"updated": task_id}


@router.put("/deliveries/{delivery_id}/checks")
async def set_manual_check(
    delivery_id: str,
    data: ManualCheckSet,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    async with get_session() as session:
        await set_manual_check_core(
            session,
            delivery_id=delivery_id,
            org_id=auth.org_id,
            data=data,
            user_id=auth.user_id,
        )
        await session.commit()
        return {"check_key": data.check_key, "checked": data.checked}


@router.post(
    "/deliveries/{delivery_id}/finalize", response_model=DeliveryBoardResponse
)
async def finalize_delivery(
    delivery_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> DeliveryBoardResponse:
    async with get_session() as session:
        board = await finalize_delivery_core(
            session,
            delivery_id=delivery_id,
            org_id=auth.org_id,
            user_id=auth.user_id,
        )
        await session.commit()
        return board


@router.get("/tasks/{task_id}/qa-history", response_model=TaskQAHistoryResponse)
async def get_task_qa_history(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TaskQAHistoryResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        return await get_task_qa_history_core(
            session, task_id=task_id, org_id=auth.org_id
        )

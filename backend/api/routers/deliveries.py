"""Authenticated delivery-checklist endpoints (docs/delivery-design.md).

Reads need the ordinary TASKS scope; every mutation is admin-only. All
readiness state is computed in ``oddish.core.deliveries`` — these routes
only add auth and transaction boundaries.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import APIKeyScope, AuthContext, require_admin, require_auth
from models import UserModel
from oddish.core.deliveries import (
    add_delivery_tasks_core,
    create_customer_core,
    create_delivery_core,
    delete_delivery_core,
    finalize_delivery_core,
    get_delivery_board_core,
    get_task_qa_history_core,
    list_customers_core,
    list_deliveries_core,
    patch_delivery_core,
    remove_delivery_task_core,
    set_manual_check_core,
)
from oddish.db import get_session
from oddish.schemas import (
    CustomerCreate,
    CustomerResponse,
    DeliveryBoardResponse,
    DeliveryCreate,
    DeliveryListItem,
    DeliveryPatch,
    DeliveryResponse,
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


@router.get("/customers", response_model=list[CustomerResponse])
async def list_customers(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[CustomerResponse]:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        customers = await list_customers_core(session, org_id=auth.org_id)
        return [CustomerResponse.model_validate(c) for c in customers]


@router.post("/customers", response_model=CustomerResponse)
async def create_customer(
    data: CustomerCreate,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CustomerResponse:
    async with get_session() as session:
        customer = await create_customer_core(
            session, org_id=auth.org_id, name=data.name
        )
        await session.commit()
        return CustomerResponse.model_validate(customer)


async def _fill_user_names(
    session: AsyncSession, org_id: str, board: DeliveryBoardResponse
) -> None:
    """Replace bare user ids with display names for the reader.

    The core stores and returns ids only; this hosted layer owns the user
    directory, so it resolves them at read time. An id without a user row
    stays as it is and the UI falls back to the id.
    """
    ids: set[str] = set()
    for row in board.tasks:
        for check in row.checks:
            if check.checked_by_user_id:
                ids.add(check.checked_by_user_id)
        for defect in row.defects:
            if defect.acknowledged_by_user_id:
                ids.add(defect.acknowledged_by_user_id)
    for check in board.delivery_checks:
        if check.checked_by_user_id:
            ids.add(check.checked_by_user_id)
    if not ids:
        return
    rows = await session.execute(
        select(
            UserModel.id,
            UserModel.name,
            UserModel.github_username,
            UserModel.email,
        ).where(UserModel.id.in_(ids), UserModel.org_id == org_id)
    )
    names: dict[str, str] = {}
    for user_id, name, handle, email in rows.all():
        # Same fallback chain as the dashboard: name, else @handle, else
        # the email local part. Never the full address.
        display = (name or "").strip()
        if not display:
            safe_handle = (handle or "").strip().lstrip("@")
            display = f"@{safe_handle}" if safe_handle else ""
        if not display:
            display = (email or "").split("@", 1)[0].strip()
        if display:
            names[user_id] = display
    for row in board.tasks:
        for check in row.checks:
            check.checked_by_name = names.get(check.checked_by_user_id or "")
        for defect in row.defects:
            defect.acknowledged_by_name = names.get(
                defect.acknowledged_by_user_id or ""
            )
    for check in board.delivery_checks:
        check.checked_by_name = names.get(check.checked_by_user_id or "")


@router.get("/deliveries/{delivery_id}", response_model=DeliveryBoardResponse)
async def get_delivery_board(
    delivery_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> DeliveryBoardResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        board = await get_delivery_board_core(
            session, delivery_id=delivery_id, org_id=auth.org_id
        )
        await _fill_user_names(session, auth.org_id, board)
        return board


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

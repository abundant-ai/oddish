"""Standalone-server delivery routes (docs/delivery-design.md).

The self-hosted twin of ``backend/api/routers/deliveries.py``: same core
calls, no auth layer, ``org_id=None`` (single-tenant rows).
"""

from __future__ import annotations

from fastapi import APIRouter

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
async def create_delivery(data: DeliveryCreate) -> DeliveryResponse:
    async with get_session() as session:
        delivery = await create_delivery_core(
            session, data=data, org_id=None, user_id=None
        )
        await session.commit()
        return DeliveryResponse.model_validate(delivery)


@router.get("/deliveries", response_model=list[DeliveryListItem])
async def list_deliveries() -> list[DeliveryListItem]:
    async with get_session() as session:
        return await list_deliveries_core(session, org_id=None)


@router.get("/customers", response_model=list[CustomerResponse])
async def list_customers() -> list[CustomerResponse]:
    async with get_session() as session:
        customers = await list_customers_core(session, org_id=None)
        return [CustomerResponse.model_validate(c) for c in customers]


@router.post("/customers", response_model=CustomerResponse)
async def create_customer(data: CustomerCreate) -> CustomerResponse:
    async with get_session() as session:
        customer = await create_customer_core(session, org_id=None, name=data.name)
        await session.commit()
        return CustomerResponse.model_validate(customer)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryBoardResponse)
async def get_delivery_board(delivery_id: str) -> DeliveryBoardResponse:
    async with get_session() as session:
        return await get_delivery_board_core(
            session, delivery_id=delivery_id, org_id=None
        )


@router.patch("/deliveries/{delivery_id}", response_model=DeliveryResponse)
async def patch_delivery(delivery_id: str, data: DeliveryPatch) -> DeliveryResponse:
    async with get_session() as session:
        delivery = await patch_delivery_core(
            session, delivery_id=delivery_id, org_id=None, data=data
        )
        await session.commit()
        return DeliveryResponse.model_validate(delivery)


@router.delete("/deliveries/{delivery_id}")
async def delete_delivery(delivery_id: str) -> dict:
    async with get_session() as session:
        await delete_delivery_core(session, delivery_id=delivery_id, org_id=None)
        await session.commit()
        return {"deleted": delivery_id}


@router.post("/deliveries/{delivery_id}/tasks")
async def add_delivery_tasks(delivery_id: str, data: DeliveryTasksAdd) -> dict:
    async with get_session() as session:
        added = await add_delivery_tasks_core(
            session, delivery_id=delivery_id, org_id=None, data=data
        )
        await session.commit()
        return {"added": added}


@router.delete("/deliveries/{delivery_id}/tasks/{task_id}")
async def remove_delivery_task(delivery_id: str, task_id: str) -> dict:
    async with get_session() as session:
        await remove_delivery_task_core(
            session, delivery_id=delivery_id, org_id=None, task_id=task_id
        )
        await session.commit()
        return {"removed": task_id}


@router.put("/deliveries/{delivery_id}/checks")
async def set_manual_check(delivery_id: str, data: ManualCheckSet) -> dict:
    async with get_session() as session:
        await set_manual_check_core(
            session, delivery_id=delivery_id, org_id=None, data=data, user_id=None
        )
        await session.commit()
        return {"check_key": data.check_key, "checked": data.checked}


@router.post(
    "/deliveries/{delivery_id}/finalize", response_model=DeliveryBoardResponse
)
async def finalize_delivery(delivery_id: str) -> DeliveryBoardResponse:
    async with get_session() as session:
        board = await finalize_delivery_core(
            session, delivery_id=delivery_id, org_id=None, user_id=None
        )
        await session.commit()
        return board


@router.get("/tasks/{task_id}/qa-history", response_model=TaskQAHistoryResponse)
async def get_task_qa_history(task_id: str) -> TaskQAHistoryResponse:
    async with get_session() as session:
        return await get_task_qa_history_core(session, task_id=task_id, org_id=None)

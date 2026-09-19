"""Standalone-server delivery routes (docs/delivery-design.md).

The self-hosted twin of ``backend/api/routers/deliveries.py``: same core
calls, no auth layer, ``org_id=None`` (single-tenant rows).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from oddish.core.deliveries import (
    add_delivery_tasks_core,
    claim_delivery_qa_core,
    create_customer_core,
    create_delivery_core,
    delete_delivery_core,
    finalize_delivery_core,
    get_delivery_board_core,
    get_delivery_task_core,
    get_task_qa_history_core,
    list_customers_core,
    list_deliveries_core,
    patch_delivery_core,
    patch_delivery_qa_work_core,
    remove_delivery_task_core,
    set_manual_check_core,
)
from oddish.core.delivery_view import delivery_page, delivery_selection
from oddish.core.ingest.delivery_apply import (
    DEFAULT_MAX_INVENTORY_AGE,
    apply_plan_core,
    list_import_receipts_core,
    load_json_document,
    parse_customer_map,
    parse_plan,
)
from oddish.core.ingest.delivery_inventory import export_inventory_core
from oddish.db import get_read_session, get_session
from oddish.schemas import (
    CustomerCreate,
    CustomerResponse,
    DeliveryBoardResponse,
    DeliveryCreate,
    DeliveryListItem,
    DeliveryPageResponse,
    DeliveryPatch,
    DeliveryResponse,
    DeliverySelectionItem,
    DeliveryTasksAdd,
    DeliveryTaskBoardRow,
    DeliveryViewQuery,
    HistoryImportReceipt,
    ManualCheckSet,
    QAWorkClaim,
    QAWorkPatch,
    TaskInventoryResponse,
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


# Declared before ``/deliveries/{delivery_id}`` so these literal paths win.
@router.get("/deliveries/task-inventory", response_model=TaskInventoryResponse)
async def get_task_inventory() -> TaskInventoryResponse:
    async with get_read_session() as session:
        inventory = await export_inventory_core(session, org_id=None)
        return TaskInventoryResponse.model_validate(inventory)


@router.get("/deliveries/history-imports", response_model=list[HistoryImportReceipt])
async def list_history_imports(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[HistoryImportReceipt]:
    async with get_read_session() as session:
        receipts = await list_import_receipts_core(session, org_id=None, limit=limit)
        return [HistoryImportReceipt.model_validate(r) for r in receipts]


@router.post("/deliveries/history-imports", response_model=HistoryImportReceipt)
async def import_delivery_history(
    plan: Annotated[UploadFile, File()],
    inventory: Annotated[UploadFile, File()],
    apply: Annotated[bool, Form()] = False,
    customer: Annotated[list[str] | None, Form()] = None,
    max_inventory_age_hours: Annotated[float | None, Form(gt=0)] = None,
) -> HistoryImportReceipt:
    try:
        plan_doc, plan_hash = await asyncio.to_thread(parse_plan, await plan.read())
        inventory_doc = await asyncio.to_thread(
            load_json_document, await inventory.read(), "inventory"
        )
        customer_map = parse_customer_map(customer or [])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with get_session() as session:
        try:
            receipt = await apply_plan_core(
                session,
                plan=plan_doc,
                inventory=inventory_doc,
                org_id=None,
                mode="apply" if apply else "preview",
                plan_hash=plan_hash,
                customer_map=customer_map,
                user_id="local",
                max_inventory_age=(
                    timedelta(hours=max_inventory_age_hours)
                    if max_inventory_age_hours
                    else DEFAULT_MAX_INVENTORY_AGE
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=f"plan is malformed: {exc}"
            ) from exc
        await session.commit()
        return HistoryImportReceipt.model_validate(receipt)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryBoardResponse)
async def get_delivery_board(delivery_id: str) -> DeliveryBoardResponse:
    async with get_session() as session:
        board = await get_delivery_board_core(
            session, delivery_id=delivery_id, org_id=None
        )
        board.qa_viewer_user_id = "local"
        return board


@router.get("/deliveries/{delivery_id}/view", response_model=DeliveryPageResponse)
async def get_delivery_view(
    delivery_id: str,
    view: Annotated[DeliveryViewQuery, Query()],
) -> DeliveryPageResponse:
    async with get_read_session() as session:
        board = await get_delivery_board_core(
            session, delivery_id=delivery_id, org_id=None, include_details=False
        )
        board.qa_viewer_user_id = "local"
        return await delivery_page(session, board, view)


@router.get(
    "/deliveries/{delivery_id}/selection", response_model=list[DeliverySelectionItem]
)
async def get_delivery_selection(
    delivery_id: str,
    view: Annotated[DeliveryViewQuery, Query()],
) -> list[DeliverySelectionItem]:
    async with get_read_session() as session:
        board = await get_delivery_board_core(
            session, delivery_id=delivery_id, org_id=None, include_details=False
        )
        board.qa_viewer_user_id = "local"
        return delivery_selection(board, view)


@router.get(
    "/deliveries/{delivery_id}/tasks/{task_id}", response_model=DeliveryTaskBoardRow
)
async def get_delivery_task(delivery_id: str, task_id: str) -> DeliveryTaskBoardRow:
    async with get_read_session() as session:
        return await get_delivery_task_core(
            session, delivery_id=delivery_id, org_id=None, task_id=task_id
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
            session, delivery_id=delivery_id, org_id=None, data=data, user_id="local"
        )
        await session.commit()
        return {"check_key": data.check_key, "checked": data.checked}


@router.post("/deliveries/{delivery_id}/finalize", response_model=DeliveryBoardResponse)
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


@router.post("/deliveries/{delivery_id}/qa-work/claim")
async def claim_qa_work(delivery_id: str, data: QAWorkClaim) -> dict:
    async with get_session() as session:
        claimed = await claim_delivery_qa_core(
            session,
            delivery_id=delivery_id,
            org_id=None,
            user_id="local",
            data=data,
        )
        await session.commit()
        return {"claimed_version_ids": claimed}


@router.patch("/deliveries/{delivery_id}/qa-work")
async def patch_qa_work(delivery_id: str, data: QAWorkPatch) -> dict:
    async with get_session() as session:
        await patch_delivery_qa_work_core(
            session,
            delivery_id=delivery_id,
            org_id=None,
            user_id="local",
            is_admin=True,
            data=data,
        )
        await session.commit()
        return {"updated": data.version_id}

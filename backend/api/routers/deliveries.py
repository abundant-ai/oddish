"""Authenticated delivery-checklist endpoints (docs/delivery-design.md).

Reads and work coordination need the ordinary TASKS scope; readiness mutations
are admin-only. Readiness state is computed in ``oddish.core.deliveries`` — these routes
only add auth and transaction boundaries.
"""

import asyncio
from datetime import timedelta
from typing import Annotated

from auth import (
    APIKeyScope,
    AuthContext,
    authorized_read_session,
    get_auth_context,
    require_admin,
    require_auth,
)
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from models import UserModel, UserRole
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    async with get_read_session() as session:
        return await list_deliveries_core(session, org_id=auth.org_id)


@router.get("/customers", response_model=list[CustomerResponse])
async def list_customers(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[CustomerResponse]:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_read_session() as session:
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
    session: AsyncSession,
    org_id: str,
    board: DeliveryBoardResponse | DeliveryTaskBoardRow,
) -> None:
    """Replace bare user ids with display names for the reader.

    The core stores and returns ids only; this hosted layer owns the user
    directory, so it resolves them at read time. An id without a user row
    stays as it is and the UI falls back to the id.
    """
    tasks = board.tasks if isinstance(board, DeliveryBoardResponse) else [board]
    checks = board.delivery_checks if isinstance(board, DeliveryBoardResponse) else []
    ids: set[str] = set()
    for row in tasks:
        if row.qa_work.owner_user_id:
            ids.add(row.qa_work.owner_user_id)
        for check in row.checks:
            if check.checked_by_user_id:
                ids.add(check.checked_by_user_id)
        for defect in row.defects:
            if defect.acknowledged_by_user_id:
                ids.add(defect.acknowledged_by_user_id)
    for check in checks:
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
    for row in tasks:
        row.qa_owner_name = names.get(row.qa_work.owner_user_id or "")
        for check in row.checks:
            check.checked_by_name = names.get(check.checked_by_user_id or "")
        for defect in row.defects:
            defect.acknowledged_by_name = names.get(
                defect.acknowledged_by_user_id or ""
            )
    for check in checks:
        check.checked_by_name = names.get(check.checked_by_user_id or "")


# Declared before ``/deliveries/{delivery_id}`` so these literal paths win.
@router.get("/deliveries/task-inventory", response_model=TaskInventoryResponse)
async def get_task_inventory(
    request: Request,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> TaskInventoryResponse:
    """Task identities for the delivery metadata planner, retired tasks included."""
    async with authorized_read_session(request, auth) as session:
        auth.require_scope(APIKeyScope.TASKS)
        inventory = await export_inventory_core(session, org_id=auth.org_id)
        return TaskInventoryResponse.model_validate(inventory)


@router.get("/deliveries/history-imports", response_model=list[HistoryImportReceipt])
async def list_history_imports(
    request: Request,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[HistoryImportReceipt]:
    async with authorized_read_session(request, auth) as session:
        auth.require_scope(APIKeyScope.TASKS)
        receipts = await list_import_receipts_core(
            session, org_id=auth.org_id, limit=limit
        )
        return [HistoryImportReceipt.model_validate(r) for r in receipts]


@router.post("/deliveries/history-imports", response_model=HistoryImportReceipt)
async def import_delivery_history(
    auth: Annotated[AuthContext, Depends(require_admin)],
    plan: Annotated[UploadFile, File()],
    inventory: Annotated[UploadFile, File()],
    apply: Annotated[bool, Form()] = False,
    customer: Annotated[list[str] | None, Form()] = None,
    max_inventory_age_hours: Annotated[float | None, Form(gt=0)] = None,
) -> HistoryImportReceipt:
    """Preview (default) or apply a reviewed delivery metadata plan.

    Admin only: ``apply`` writes task aliases, metadata assertions, and
    delivery history, and is accepted only after a recent preview of the same
    plan. Every run, including a rejected one, records a receipt.
    """
    plan_doc, plan_hash = await _read_plan_upload(plan)
    inventory_doc = await _read_json_upload(inventory, "inventory")
    try:
        customer_map = parse_customer_map(customer or [])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    max_age = (
        timedelta(hours=max_inventory_age_hours)
        if max_inventory_age_hours
        else DEFAULT_MAX_INVENTORY_AGE
    )
    async with get_session() as session:
        try:
            receipt = await apply_plan_core(
                session,
                plan=plan_doc,
                inventory=inventory_doc,
                org_id=auth.org_id,
                mode="apply" if apply else "preview",
                plan_hash=plan_hash,
                customer_map=customer_map,
                user_id=auth.user_id,
                max_inventory_age=max_age,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=f"plan is malformed: {exc}"
            ) from exc
        await session.commit()
        return HistoryImportReceipt.model_validate(receipt)


# The real plan is under 20 MB; this bounds a runaway upload, not a
# legitimate one. Parsing runs off the event loop so other requests on the
# container are not stalled by a large document.
_MAX_IMPORT_UPLOAD_BYTES = 128 * 1024 * 1024


async def _read_upload(upload: UploadFile, label: str) -> bytes:
    data = await upload.read(_MAX_IMPORT_UPLOAD_BYTES + 1)
    if len(data) > _MAX_IMPORT_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} upload is too large")
    return data


async def _read_json_upload(upload: UploadFile, label: str) -> dict:
    data = await _read_upload(upload, label)
    try:
        return await asyncio.to_thread(load_json_document, data, label)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _read_plan_upload(upload: UploadFile) -> tuple[dict, str]:
    data = await _read_upload(upload, "plan")
    try:
        return await asyncio.to_thread(parse_plan, data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/deliveries/{delivery_id}", response_model=DeliveryBoardResponse)
async def get_delivery_board(
    request: Request,
    delivery_id: str,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> DeliveryBoardResponse:
    async with authorized_read_session(request, auth) as session:
        auth.require_scope(APIKeyScope.TASKS)
        board = await get_delivery_board_core(
            session, delivery_id=delivery_id, org_id=auth.org_id
        )
        board.qa_viewer_user_id = auth.user_id
        await _fill_user_names(session, auth.org_id, board)
        return board


@router.get("/deliveries/{delivery_id}/view", response_model=DeliveryPageResponse)
async def get_delivery_view(
    request: Request,
    delivery_id: str,
    view: Annotated[DeliveryViewQuery, Query()],
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> DeliveryPageResponse:
    async with authorized_read_session(request, auth) as session:
        auth.require_scope(APIKeyScope.TASKS)
        board = await get_delivery_board_core(
            session, delivery_id=delivery_id, org_id=auth.org_id, include_details=False
        )
        board.qa_viewer_user_id = auth.user_id
        await _fill_user_names(session, auth.org_id, board)
        return await delivery_page(session, board, view)


@router.get(
    "/deliveries/{delivery_id}/selection", response_model=list[DeliverySelectionItem]
)
async def get_delivery_selection(
    request: Request,
    delivery_id: str,
    view: Annotated[DeliveryViewQuery, Query()],
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> list[DeliverySelectionItem]:
    async with authorized_read_session(request, auth) as session:
        auth.require_scope(APIKeyScope.TASKS)
        board = await get_delivery_board_core(
            session, delivery_id=delivery_id, org_id=auth.org_id, include_details=False
        )
        board.qa_viewer_user_id = auth.user_id
        await _fill_user_names(session, auth.org_id, board)
        return delivery_selection(board, view)


@router.get(
    "/deliveries/{delivery_id}/tasks/{task_id}", response_model=DeliveryTaskBoardRow
)
async def get_delivery_task(
    request: Request,
    delivery_id: str,
    task_id: str,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> DeliveryTaskBoardRow:
    async with authorized_read_session(request, auth) as session:
        auth.require_scope(APIKeyScope.TASKS)
        row = await get_delivery_task_core(
            session, delivery_id=delivery_id, org_id=auth.org_id, task_id=task_id
        )
        await _fill_user_names(session, auth.org_id, row)
        return row


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
        await delete_delivery_core(session, delivery_id=delivery_id, org_id=auth.org_id)
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


@router.post("/deliveries/{delivery_id}/finalize", response_model=DeliveryBoardResponse)
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
    request: Request,
    task_id: str,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> TaskQAHistoryResponse:
    async with authorized_read_session(request, auth) as session:
        auth.require_scope(APIKeyScope.TASKS)
        return await get_task_qa_history_core(
            session, task_id=task_id, org_id=auth.org_id
        )


@router.post("/deliveries/{delivery_id}/qa-work/claim")
async def claim_qa_work(
    delivery_id: str,
    data: QAWorkClaim,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="QA claims require a user identity")
    async with get_session() as session:
        claimed = await claim_delivery_qa_core(
            session,
            delivery_id=delivery_id,
            org_id=auth.org_id,
            user_id=auth.user_id,
            data=data,
        )
        await session.commit()
        return {"claimed_version_ids": claimed}


@router.patch("/deliveries/{delivery_id}/qa-work")
async def patch_qa_work(
    delivery_id: str,
    data: QAWorkPatch,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="QA work requires a user identity")
    async with get_session() as session:
        await patch_delivery_qa_work_core(
            session,
            delivery_id=delivery_id,
            org_id=auth.org_id,
            user_id=auth.user_id,
            is_admin=auth.user_role == UserRole.ADMIN,
            data=data,
        )
        await session.commit()
        return {"updated": data.version_id}

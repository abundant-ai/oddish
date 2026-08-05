"""Admin API for the model aliases rendered on published share pages."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError

from auth import AuthContext, can_manage_api_keys, require_admin
from auth.permissions import require_operator_org
from oddish.db import ModelDisplayNameModel, get_session, utcnow
from pg_errors import is_undefined_table_error

router = APIRouter(prefix="/admin/model-display-names", tags=["Admin"])


class ModelDisplayNameResponse(BaseModel):
    id: str
    model_name: str
    display_name: str
    created_by: str | None
    created_at: str
    updated_at: str


class ModelDisplayNameRequest(BaseModel):
    model_name: str
    display_name: str


class UpdateModelDisplayNameRequest(BaseModel):
    display_name: str


def _response(row: ModelDisplayNameModel) -> ModelDisplayNameResponse:
    return ModelDisplayNameResponse(
        id=row.id,
        model_name=row.model_name,
        display_name=row.display_name,
        created_by=row.created_by_user_id,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _require_manage(auth: AuthContext) -> None:
    require_operator_org(auth)
    if not can_manage_api_keys(auth):
        raise HTTPException(
            status_code=403,
            detail="Only organization admins may edit model display names",
        )


def _unavailable(exc: ProgrammingError) -> HTTPException:
    if not is_undefined_table_error(exc):
        raise exc
    return HTTPException(
        status_code=503,
        detail=(
            "Model display names are not available yet (schema is still "
            "migrating). Try again shortly."
        ),
    )


def _clean(model_name: str, display_name: str) -> tuple[str, str]:
    model_name = model_name.strip()
    display_name = display_name.strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="model_name must not be empty")
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name must not be empty")
    return model_name, display_name


@router.get("", response_model=list[ModelDisplayNameResponse])
async def list_model_display_names(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[ModelDisplayNameResponse]:
    require_operator_org(auth)
    try:
        async with get_session() as session:
            result = await session.execute(
                select(ModelDisplayNameModel).order_by(
                    ModelDisplayNameModel.model_name.asc()
                )
            )
            return [_response(row) for row in result.scalars().all()]
    except ProgrammingError as exc:
        raise _unavailable(exc) from exc


@router.post("", response_model=ModelDisplayNameResponse)
async def set_model_display_name(
    request: ModelDisplayNameRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ModelDisplayNameResponse:
    """Create the alias, or retarget the live one for the same model."""
    _require_manage(auth)
    model_name, display_name = _clean(request.model_name, request.display_name)

    try:
        async with get_session() as session:
            existing = (
                await session.execute(
                    select(ModelDisplayNameModel).where(
                        ModelDisplayNameModel.model_name == model_name
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.display_name = display_name
                await session.commit()
                return _response(existing)

            row = ModelDisplayNameModel(
                model_name=model_name,
                display_name=display_name,
                created_by_user_id=auth.user_id,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                raise HTTPException(
                    status_code=409, detail="that model already has a display name"
                )
            return _response(row)
    except ProgrammingError as exc:
        raise _unavailable(exc) from exc


@router.put("/{name_id}", response_model=ModelDisplayNameResponse)
async def update_model_display_name(
    name_id: str,
    request: UpdateModelDisplayNameRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ModelDisplayNameResponse:
    _require_manage(auth)
    display_name = request.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name must not be empty")

    try:
        async with get_session() as session:
            row = (
                await session.execute(
                    select(ModelDisplayNameModel).where(
                        ModelDisplayNameModel.id == name_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="display name not found")
            row.display_name = display_name
            await session.commit()
            return _response(row)
    except ProgrammingError as exc:
        raise _unavailable(exc) from exc


@router.delete("/{name_id}")
async def remove_model_display_name(
    name_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    _require_manage(auth)
    try:
        async with get_session() as session:
            row = (
                await session.execute(
                    select(ModelDisplayNameModel).where(
                        ModelDisplayNameModel.id == name_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="display name not found")
            row.deleted_at = utcnow()
            await session.commit()
    except ProgrammingError as exc:
        raise _unavailable(exc) from exc
    return {"deleted": name_id}

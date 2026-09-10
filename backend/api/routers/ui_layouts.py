"""Personal drawer preferences, scoped to the signed-in organization membership."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from auth import AuthContext, AuthMethod, require_auth
from models import UserUiLayoutModel
from oddish.db import get_read_session, get_session, utcnow

router = APIRouter(prefix="/users/me/ui-layouts", tags=["Preferences"])
LayoutKey = Literal["experiment.trial-drawer"]


class TrialDrawerLayout(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    # Unset width uses the existing single-pane / two-pane defaults.
    preferredWidthPx: float | None = Field(default=None, ge=420, le=16384)
    maximized: bool = False
    taskPanePercent: float = Field(default=42, ge=15, le=85)
    showTask: bool = True
    showTrial: bool = True

    @model_validator(mode="after")
    def visible_pane(self) -> "TrialDrawerLayout":
        if not self.showTask and not self.showTrial:
            raise ValueError("At least one pane must be visible")
        return self


def require_layout_user(auth: Annotated[AuthContext, Depends(require_auth)]) -> str:
    if auth.method != AuthMethod.CLERK_JWT or not auth.user_id:
        raise HTTPException(
            status_code=403, detail="Layout preferences require user login"
        )
    return auth.user_id


@router.get("/{layout_key}", response_model=TrialDrawerLayout)
async def get_layout(
    layout_key: LayoutKey,
    user_id: Annotated[str, Depends(require_layout_user)],
) -> TrialDrawerLayout:
    async with get_read_session() as session:
        value = await session.scalar(
            select(UserUiLayoutModel.value).where(
                UserUiLayoutModel.user_id == user_id,
                UserUiLayoutModel.layout_key == layout_key,
            )
        )
    return (
        TrialDrawerLayout()
        if value is None
        else TrialDrawerLayout.model_validate(value)
    )


@router.put("/{layout_key}", response_model=TrialDrawerLayout)
async def put_layout(
    layout_key: LayoutKey,
    payload: TrialDrawerLayout,
    user_id: Annotated[str, Depends(require_layout_user)],
) -> TrialDrawerLayout:
    values = {"value": payload.model_dump(), "updated_at": utcnow()}
    async with get_session() as session:
        await session.execute(
            insert(UserUiLayoutModel)
            .values(user_id=user_id, layout_key=layout_key, **values)
            .on_conflict_do_update(
                index_elements=["user_id", "layout_key"], set_=values
            )
        )
    return payload

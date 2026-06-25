from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from auth import APIKeyScope, AuthContext, require_auth
from models import UserModel
from oddish.core.cost_breakdown import get_cost_breakdown_core
from oddish.db import get_session

router = APIRouter(tags=["Cost"])

# Bound the window so this view never issues an unbounded trials scan. Matches
# the 60d ceiling the usage time picker already clamps custom ranges to.
_MAX_USAGE_MINUTES = 86400
_DEFAULT_USAGE_MINUTES = 1440


def _owner_label(user: UserModel) -> str:
    return user.github_username or user.email or user.name or user.id


@router.get("/cost-breakdown")
async def get_cost_breakdown(
    auth: Annotated[AuthContext, Depends(require_auth)],
    usage_minutes: int = Query(
        _DEFAULT_USAGE_MINUTES,
        ge=1,
        le=_MAX_USAGE_MINUTES,
        description="Bounded lookback window in minutes (<= 60d).",
    ),
) -> dict[str, Any]:
    """Finest-grain trial cost breakdown (model x experiment, with owner).

    Returns one row per (model, experiment) carrying the experiment owner so
    the frontend can re-aggregate by model, experiment, or user without
    refetching. ``owner_user_id`` is resolved here to a display label because
    the ``users`` table lives in the backend layer.
    """
    auth.require_scope(APIKeyScope.READ)

    async with get_session() as session:
        rows = await get_cost_breakdown_core(
            session,
            org_id=auth.org_id,
            usage_minutes=usage_minutes,
        )

        # Resolve the experiment owners referenced by the breakdown to display
        # labels in one pass over the org's users.
        owner_ids = {
            row["owner_user_id"] for row in rows if row.get("owner_user_id")
        }
        labels: dict[str, str] = {}
        if owner_ids:
            users_result = await session.execute(
                select(UserModel).where(
                    UserModel.org_id == auth.org_id,
                    UserModel.id.in_(owner_ids),
                )
            )
            labels = {u.id: _owner_label(u) for u in users_result.scalars().all()}

    enriched = []
    for row in rows:
        owner_id = row.get("owner_user_id")
        enriched.append(
            {
                **row,
                "owner_label": labels.get(owner_id, "Unassigned")
                if owner_id
                else "Unassigned",
            }
        )

    return {"usage_minutes": usage_minutes, "rows": enriched}

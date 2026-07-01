"""Authenticated live submission-load snapshot."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import AuthContext, require_auth
from oddish.core.admin import LoadSnapshot, get_cached_load_snapshot

router = APIRouter(tags=["Load"])


@router.get("/load", response_model=LoadSnapshot)
async def get_load(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> LoadSnapshot:
    """Live submission-load snapshot (auth-gated, ~5s cached, single-flighted)."""
    return await get_cached_load_snapshot()

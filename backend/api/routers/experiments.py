from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from oddish.core.experiment_backfill import backfill_experiment_core
from oddish.core.experiment_cells import (
    add_cell_core,
    delete_cell_core,
    list_cells_core,
    resolve_experiment_core,
    update_cell_core,
)
from oddish.db import get_session
from oddish.schemas import (
    ExperimentBackfillResponse,
    ExperimentCellCreateRequest,
    ExperimentCellResponse,
    ExperimentCellUpdateRequest,
    ResolvedExperimentResponse,
)

from auth import APIKeyScope, AuthContext, require_admin, require_auth


router = APIRouter(tags=["Experiments"])


@router.get(
    "/experiments/{experiment_id}/cells",
    response_model=list[ExperimentCellResponse],
)
async def list_experiment_cells(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[ExperimentCellResponse]:
    """List the cells in an experiment's selection (no evidence join)."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_cells_core(
            session, experiment_id=experiment_id, org_id=auth.org_id
        )


@router.get(
    "/experiments/{experiment_id}/resolved",
    response_model=ResolvedExperimentResponse,
)
async def resolve_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ResolvedExperimentResponse:
    """Return cells joined with current trial evidence (the matrix view)."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await resolve_experiment_core(
            session, experiment_id=experiment_id, org_id=auth.org_id
        )


@router.post(
    "/experiments/{experiment_id}/cells",
    response_model=ExperimentCellResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_experiment_cell(
    experiment_id: str,
    payload: ExperimentCellCreateRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentCellResponse:
    """Append a cell to an experiment. Idempotent on the equivalence key
    (existing cell with the same (task_version, agent) gets its target
    bumped instead of duplicated)."""
    async with get_session() as session:
        return await add_cell_core(
            session,
            experiment_id=experiment_id,
            payload=payload,
            org_id=auth.org_id,
        )


@router.patch(
    "/experiments/{experiment_id}/cells/{cell_id}",
    response_model=ExperimentCellResponse,
)
async def update_experiment_cell(
    experiment_id: str,
    cell_id: str,
    payload: ExperimentCellUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentCellResponse:
    """Bump ``target_n_trials`` on an existing cell."""
    async with get_session() as session:
        return await update_cell_core(
            session,
            experiment_id=experiment_id,
            cell_id=cell_id,
            payload=payload,
            org_id=auth.org_id,
        )


@router.post(
    "/experiments/{experiment_id}/backfill",
    response_model=ExperimentBackfillResponse,
)
async def backfill_experiment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentBackfillResponse:
    """Enqueue trials to fill the gaps on every cell of an experiment.

    Creates a Job(kind=experiment_backfill); the trials it produces show
    up in the matrix as their corresponding cells fill in.
    """
    async with get_session() as session:
        return await backfill_experiment_core(
            session,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            user_id=None,
        )


@router.delete(
    "/experiments/{experiment_id}/cells/{cell_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_experiment_cell(
    experiment_id: str,
    cell_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> None:
    """Drop a cell from an experiment."""
    async with get_session() as session:
        await delete_cell_core(
            session,
            experiment_id=experiment_id,
            cell_id=cell_id,
            org_id=auth.org_id,
        )

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from oddish.core.experiment_backfill import backfill_experiment_core
from oddish.core.experiment_cells import (
    bulk_cells_core,
    create_experiment_core,
    list_cell_trials_core,
    list_experiments_core,
    list_known_agents_core,
    resolve_experiment_core,
    update_cell_core,
)
from oddish.db import get_session
from oddish.schemas import (
    ExperimentBackfillResponse,
    ExperimentBulkCellRequest,
    ExperimentBulkCellResponse,
    ExperimentCellResponse,
    ExperimentCellUpdateRequest,
    ExperimentCreateRequest,
    ExperimentCreateResponse,
    ResolvedExperimentResponse,
)

from auth import APIKeyScope, AuthContext, require_admin, require_auth


router = APIRouter(tags=["Experiments"])


@router.get("/agents/known")
async def list_known_agents(
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = 200,
) -> list[dict]:
    """List distinct agent identities seen in trials, for builder pickers."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_known_agents_core(
            session, org_id=auth.org_id, limit=limit
        )


@router.get("/experiments")
async def list_experiments(
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List experiments visible to the org. Lightweight payload for pickers."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_experiments_core(
            session, org_id=auth.org_id, limit=limit, offset=offset
        )


@router.post(
    "/experiments",
    response_model=ExperimentCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_experiment(
    payload: ExperimentCreateRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
    dry_run: bool = False,
) -> ExperimentCreateResponse:
    """Create a new experiment with optional initial cells, and start it.

    By default, the experiment is created and a backfill is enqueued in
    the same request -- a single ``POST /experiments`` call is enough to
    spin up trials, matching the pre-task-first ``submit-and-it-runs``
    API semantics that automation relies on.

    Pass ``?dry_run=true`` to create the spec only (no trials enqueued);
    call ``POST /experiments/{id}/backfill`` later to start the work.
    """
    async with get_session() as session:
        resolved = await create_experiment_core(
            session,
            name=payload.name,
            task_version_ids=payload.task_version_ids,
            agents=payload.agents,
            target_n_trials=payload.target_n_trials,
            org_id=auth.org_id,
        )
        backfill: ExperimentBackfillResponse | None = None
        if not dry_run:
            backfill = await backfill_experiment_core(
                session,
                experiment_id=resolved.experiment_id,
                org_id=auth.org_id,
                user_id=auth.user_id,
            )
            # Re-resolve so the returned cells reflect the freshly-queued trials.
            resolved = await resolve_experiment_core(
                session,
                experiment_id=resolved.experiment_id,
                org_id=auth.org_id,
            )
        return ExperimentCreateResponse(
            **resolved.model_dump(),
            backfill=backfill,
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
    """Set the per-pair ``target_n_trials`` override for one (task, agent)
    intersection. ``cell_id`` may be a real ``experiment_cells.id`` or a
    synthetic ``"{task_version_id}:{agent_equivalence_key}"`` handle."""
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


@router.get(
    "/experiments/{experiment_id}/cells/{cell_id}/trials",
)
async def list_cell_trials(
    experiment_id: str,
    cell_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = 200,
) -> list[dict]:
    """Return the trials matching a cell's (task_version, agent) pair."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_cell_trials_core(
            session,
            experiment_id=experiment_id,
            cell_id=cell_id,
            org_id=auth.org_id,
            limit=limit,
        )


@router.post(
    "/experiments/{experiment_id}/cells/bulk",
    response_model=ExperimentBulkCellResponse,
)
async def bulk_experiment_cells(
    experiment_id: str,
    payload: ExperimentBulkCellRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ExperimentBulkCellResponse:
    """Add or remove tasks and agents in bulk; see
    ``ExperimentBulkCellRequest`` for the supported ops."""
    async with get_session() as session:
        n = await bulk_cells_core(
            session,
            experiment_id=experiment_id,
            op=payload.op,
            target_n_trials=payload.target_n_trials,
            agent_harness=payload.agent_harness,
            agent_model=payload.agent_model,
            agent_provider=payload.agent_provider,
            task_version_id=payload.task_version_id,
            org_id=auth.org_id,
        )
        return ExperimentBulkCellResponse(op=payload.op, cells_changed=n)

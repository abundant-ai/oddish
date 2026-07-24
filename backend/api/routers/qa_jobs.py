"""REST surface for QA job assignments.

Thin wrapper over ``oddish.core.qa_assignments``: authenticate, resolve the
scope, delegate, commit, serialize. Nothing here executes an assignment --
creating rows is all this layer does; the execution cutover is a later change.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from api.scopes import resolve_read_scope, resolve_write_scope
from auth import APIKeyScope, AuthContext, require_auth
from auth.permissions import assert_org_access, require_operator_org
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.config import settings
from oddish.core.prompts import find_prompt_row_core, resolve_prompt_core
from oddish.core.qa_assignments import (
    GLOBAL_SCOPE_TYPE,
    ResolvedAssignment,
    delete_qa_assignment_core,
    get_qa_assignment_core,
    list_qa_assignments_core,
    qa_assignment_status_core,
    resolve_qa_assignments_core,
    scope_label,
    upsert_qa_assignment_core,
)
from oddish.db import QAStage, TrialModel, get_session
from oddish.schemas import (
    QAJobAssignRequest,
    QAJobResponse,
    QAJobStatusResponse,
    QAJobStatusRow,
)

router = APIRouter(tags=["QA Jobs"])

# Per-stage deployment defaults. Read at request time, not import time, so an
# env override still applies.
_STAGE_CLIENT_DEFAULT = {
    QAStage.PRE_TRIAL.value: LLMClientType.SANDBOX,
    QAStage.POST_TRIAL.value: LLMClientType.API,
}


def _default_model(stage: str) -> str:
    if stage == QAStage.PRE_TRIAL.value:
        return settings.pre_trial_model
    return settings.analysis_model


def _to_response(resolved: ResolvedAssignment) -> QAJobResponse:
    assignment = resolved.assignment
    return QAJobResponse(
        id=assignment.id,
        stage=assignment.stage,
        prompt_id=assignment.prompt_id,
        prompt_kind=resolved.prompt.kind,
        prompt_version=assignment.prompt_version,
        effective_version=resolved.effective_version,
        scope_type=assignment.scope_type,
        scope_id=assignment.scope_id,
        org_id=assignment.org_id,
        model=assignment.model,
        reasoning_effort=assignment.reasoning_effort,
        backend=(
            "sandbox"
            if assignment.llm_client_type == LLMClientType.SANDBOX.value
            else "api"
        ),
        allow_oddish_cli=assignment.allow_oddish_cli,
        enabled=assignment.enabled,
        inherited_from=resolved.inherited_from,
    )


async def _resolution_scopes(
    session, scope_type: str | None, scope_id: str | None, auth: AuthContext
) -> dict:
    """Map a requested scope onto ``resolve_qa_assignments_core`` arguments --
    i.e. the effective job set a subject at that scope would run.

    The union in ``resolve_qa_assignments_core`` needs the subject's whole
    ancestry, not just the immediate id, or ``list``/``status`` under-report
    the jobs that would actually fire. A trial runs inside a task and an
    experiment, so resolving at a trial must carry its task/experiment/user --
    exactly what the post-trial enqueue passes (``trial_handler``). Every other
    scope's only ancestor is the org, which is already set. A missing trial row
    falls back to the trial id alone.

    A global write is ``scope_type=None``; it carries no org context, so
    ``org_id`` is ``None`` -- otherwise a kind-based resolve would prefer the
    operator org's own override over the installation-wide prompt and bind
    that private row into a row every tenant inherits."""
    scopes = {
        "org_id": None if scope_type is None else auth.org_id,
        "user_id": None,
        "experiment_id": None,
        "task_id": None,
        "trial_id": None,
    }
    if scope_type == "user":
        scopes["user_id"] = scope_id
    elif scope_type in {"experiment", "task"}:
        scopes[f"{scope_type}_id"] = scope_id
    elif scope_type == "trial":
        scopes["trial_id"] = scope_id
        trial = await session.get(TrialModel, scope_id)
        if trial is not None:
            scopes["task_id"] = trial.task_id
            scopes["experiment_id"] = trial.experiment_id
            scopes["user_id"] = trial.billed_user_id
    return scopes


def _assignment_scope(scope_type: str | None, scope_id: str | None) -> tuple[str, str]:
    """``resolve_*_scope`` spells global as (None, None) -- the prompts
    convention. ``qa_assignments`` has NOT NULL scope columns, so translate."""
    if scope_type is None:
        return GLOBAL_SCOPE_TYPE, ""
    return scope_type, scope_id or ""


async def _resolved_at(session, scope_type, scope_id, auth, *, stage=None):
    if scope_type is None and scope_id is None:
        # A global read still resolves the global tier only.
        return await resolve_qa_assignments_core(
            session,
            stage=stage,
            org_id=None,
            user_id=None,
            experiment_id=None,
            task_id=None,
            trial_id=None,
        )
    return await resolve_qa_assignments_core(
        session,
        stage=stage,
        **(await _resolution_scopes(session, scope_type, scope_id, auth)),
    )


@router.get("/qa-jobs/status", response_model=QAJobStatusResponse)
async def qa_job_status(
    auth: Annotated[AuthContext, Depends(require_auth)],
    scope: Annotated[str | None, Query()] = None,
    scope_id: Annotated[str | None, Query()] = None,
) -> QAJobStatusResponse:
    """Run counts per effective assignment, for the UI panel."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        scope_type, resolved_scope_id = await resolve_read_scope(
            session, scope, scope_id, auth
        )
        resolved = await _resolved_at(session, scope_type, resolved_scope_id, auth)
        counts = await qa_assignment_status_core(
            session,
            assignment_ids=[r.assignment.id for r in resolved],
            org_id=auth.org_id,
        )
    return QAJobStatusResponse(
        scope=scope_label(*_assignment_scope(scope_type, resolved_scope_id)),
        jobs=[
            QAJobStatusRow(
                assignment_id=r.assignment.id,
                stage=r.stage,
                prompt_kind=r.kind,
                inherited_from=r.inherited_from,
                counts=counts.get(r.assignment.id, {}),
                total=sum(counts.get(r.assignment.id, {}).values()),
            )
            for r in resolved
        ],
    )


@router.get("/qa-jobs", response_model=list[QAJobResponse])
async def list_qa_jobs(
    auth: Annotated[AuthContext, Depends(require_auth)],
    scope: Annotated[str | None, Query()] = None,
    scope_id: Annotated[str | None, Query()] = None,
    resolved: Annotated[bool, Query()] = False,
    stage: Annotated[str | None, Query()] = None,
) -> list[QAJobResponse]:
    """``resolved=true`` returns the effective set, each row naming the scope it
    was inherited from. ``resolved=false`` returns only rows defined at exactly
    this scope, which is what an edit view needs."""
    auth.require_scope(APIKeyScope.READ)
    if stage is not None and stage not in {s.value for s in QAStage}:
        raise HTTPException(status_code=422, detail=f"unknown stage: {stage}")
    async with get_session() as session:
        scope_type, resolved_scope_id = await resolve_read_scope(
            session, scope, scope_id, auth
        )
        if resolved:
            rows = await _resolved_at(
                session, scope_type, resolved_scope_id, auth, stage=stage
            )
        else:
            assignment_scope, assignment_scope_id = _assignment_scope(
                scope_type, resolved_scope_id
            )
            rows = await list_qa_assignments_core(
                session,
                scope_type=assignment_scope,
                scope_id=assignment_scope_id,
                org_id=auth.org_id,
                stage=stage,
            )
        return [_to_response(r) for r in rows]


@router.post("/qa-jobs", response_model=QAJobResponse)
async def assign_qa_job(
    data: QAJobAssignRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
    scope: Annotated[str, Query()] = "org",
    scope_id: Annotated[str | None, Query()] = None,
) -> QAJobResponse:
    """Create or update the assignment for this scope, stage, and prompt kind.

    Global scope is gated to the platform operator by ``resolve_write_scope``:
    one org must not be able to rewrite what every other org's QA runs on.
    """
    auth.require_scope(APIKeyScope.FULL)
    async with get_session() as session:
        scope_type, resolved_scope_id = await resolve_write_scope(
            session, scope, scope_id, auth
        )
        assignment_scope, assignment_scope_id = _assignment_scope(
            scope_type, resolved_scope_id
        )

        ref = data.prompt.strip()
        # `data.prompt` may be an opaque id, which resolves across scopes and
        # so can name another org's row -- re-check it before binding to it.
        try:
            prompt, latest = await resolve_prompt_core(
                session,
                ref,
                **(
                    await _resolution_scopes(
                        session, scope_type, resolved_scope_id, auth
                    )
                ),
            )
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # Resolution skips version-less rows, so distinguish "no such
            # prompt" from "prompt exists but has nothing to run".
            empty = await find_prompt_row_core(
                session,
                ref,
                scope_type=scope_type,
                scope_id=resolved_scope_id,
                org_id=auth.org_id if scope_type is not None else None,
            )
            if empty is None:
                raise
            assert_org_access(empty, auth, detail="Prompt not found")
            raise HTTPException(
                status_code=400,
                detail=f"Prompt '{empty.kind}' has no versions to assign",
            ) from exc
        assert_org_access(prompt, auth, detail="Prompt not found")

        versions = await prompt.awaitable_attrs.versions
        if data.prompt_version is not None and not any(
            v.version == data.prompt_version for v in versions
        ):
            raise HTTPException(
                status_code=404,
                detail=f"Prompt '{prompt.kind}' has no version {data.prompt_version}",
            )

        client_type = (
            _STAGE_CLIENT_DEFAULT[data.stage]
            if data.backend is None
            else (
                LLMClientType.SANDBOX
                if data.backend == "sandbox"
                else LLMClientType.API
            )
        )
        # Runner config is a partial update: only the fields the request
        # actually carried overwrite an existing row, so a bare `disable` (and
        # a bare `assign` that re-enables) leaves a configured job's settings
        # intact instead of resetting them to stage defaults. `backend` maps to
        # llm_client_type. Omitted fields still take their defaults on a create.
        runner_fields_provided = set()
        if data.model is not None:
            runner_fields_provided.add("model")
        if data.backend is not None:
            runner_fields_provided.add("llm_client_type")
        if data.reasoning_effort is not None:
            runner_fields_provided.add("reasoning_effort")
        if data.allow_oddish_cli is not None:
            runner_fields_provided.add("allow_oddish_cli")
        if data.prompt_version is not None:
            runner_fields_provided.add("prompt_version")
        try:
            assignment = await upsert_qa_assignment_core(
                session,
                prompt=prompt,
                stage=data.stage,
                scope_type=assignment_scope,
                scope_id=assignment_scope_id,
                # Global rows carry no org_id so every tenant inherits them.
                org_id=None if scope_type is None else auth.org_id,
                model=data.model or _default_model(data.stage),
                llm_client_type=client_type.value,
                reasoning_effort=data.reasoning_effort,
                allow_oddish_cli=bool(data.allow_oddish_cli),
                prompt_version=data.prompt_version,
                enabled=data.enabled,
                created_by_user_id=auth.user_id,
                runner_fields_provided=runner_fields_provided,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()

        effective = (
            data.prompt_version if data.prompt_version is not None else latest.version
        )
        return _to_response(
            ResolvedAssignment(
                assignment=assignment,
                prompt=prompt,
                effective_version=effective,
                inherited_from=scope_label(assignment_scope, assignment_scope_id),
            )
        )


@router.delete("/qa-jobs/{assignment_id}")
async def delete_qa_job(
    assignment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Soft delete one assignment."""
    auth.require_scope(APIKeyScope.FULL)
    async with get_session() as session:
        assignment = await get_qa_assignment_core(session, assignment_id=assignment_id)
        if assignment is None:
            raise HTTPException(status_code=404, detail="QA job not found")
        assert_org_access(assignment, auth, detail="QA job not found")
        if assignment.org_id is None:
            # An installation-wide row belongs to every tenant, so FULL (a
            # per-org scope) is not enough to remove it.
            require_operator_org(auth)
        await delete_qa_assignment_core(session, assignment=assignment)
        await session.commit()
    return {"id": assignment_id, "deleted": True}

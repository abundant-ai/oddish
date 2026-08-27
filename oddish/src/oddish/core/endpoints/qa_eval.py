"""Create QA prompt replays that point at historical solver trials."""

from __future__ import annotations

import hashlib

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import is_nop_oracle_agent, settings
from oddish.core.idempotency import (
    IdempotencyConflict,
    IdempotencyStore,
    Reservation,
    compute_request_hash,
    reserve_idempotency_slot,
)
from oddish.core.quota_admission import admit_trials
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    utcnow,
)
from oddish.schemas import (
    QAEvalCreateRequest,
    QAEvalCreateResponse,
    QAEvalTrialResponse,
)
from oddish.workers.analysis_trials import build_qa_brief, create_analysis_trial

_QA_EVAL_SOURCE_STATUSES = (TrialStatus.SUCCESS, TrialStatus.FAILED)
_QA_EVAL_ROUTE = "POST /qa-evals"


async def create_qa_eval_core(
    session: AsyncSession,
    *,
    request: QAEvalCreateRequest,
    org_id: str | None,
    owner_user_id: str | None,
    billed_user_id: str | None,
    idempotency_key: str | None = None,
    idempotency_store: IdempotencyStore | None = None,
    request_hash: str | None = None,
) -> QAEvalCreateResponse:
    """Create one replay experiment and one QA trial per source trial."""
    reservation: Reservation | None = None
    if idempotency_store is not None and idempotency_key and org_id:
        try:
            reservation = await reserve_idempotency_slot(
                idempotency_store,
                org_id=org_id,
                route=_QA_EVAL_ROUTE,
                raw_key=idempotency_key,
                request_hash=request_hash or compute_request_hash(request),
                now=utcnow(),
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    source_rows = (
        (
            await session.execute(
                select(TrialModel).where(
                    TrialModel.id.in_(request.source_trial_ids),
                    TrialModel.org_id == org_id,
                )
            )
        )
        .scalars()
        .all()
    )
    source_by_id = {row.id: row for row in source_rows}
    task_ids = list(dict.fromkeys(row.task_id for row in source_rows))
    version_ids = list(
        dict.fromkeys(row.task_version_id for row in source_rows if row.task_version_id)
    )
    tasks = (
        (
            await session.execute(
                select(TaskModel).where(
                    TaskModel.id.in_(task_ids), TaskModel.org_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    versions = (
        (
            await session.execute(
                select(TaskVersionModel).where(TaskVersionModel.id.in_(version_ids))
            )
        )
        .scalars()
        .all()
        if version_ids
        else []
    )
    task_by_id = {row.id: row for row in tasks}
    version_by_id = {row.id: row for row in versions}

    invalid: list[str] = []
    for source_trial_id in request.source_trial_ids:
        source = source_by_id.get(source_trial_id)
        reason = None
        if source is None:
            reason = "not found or inaccessible"
        elif (
            source.kind != "agent"
            or source.is_probe
            or is_nop_oracle_agent(source.agent)
        ):
            reason = "not an ordinary solver trial"
        elif source.superseded_by_trial_id is not None:
            reason = "superseded"
        elif source.status not in _QA_EVAL_SOURCE_STATUSES:
            reason = f"not terminal ({source.status.value})"
        elif source.task_version_id is None:
            reason = "missing an exact task version"
        elif source.task_id not in task_by_id:
            reason = "missing its task"
        elif source.task_version_id not in version_by_id:
            reason = "missing its exact task version"
        elif version_by_id[source.task_version_id].task_id != source.task_id:
            reason = "task version belongs to another task"
        elif not version_by_id[source.task_version_id].task_s3_key:
            reason = "exact task version has no stored files"
        elif not source.has_trajectory:
            reason = "missing a stored trajectory"
        if reason:
            invalid.append(f"{source_trial_id}: {reason}")

    if invalid:
        raise HTTPException(
            status_code=409,
            detail="QA replay rejected; fix these source rows: " + "; ".join(invalid),
        )

    await admit_trials(
        session, org_id, billed_user_id, count=len(request.source_trial_ids)
    )

    canonical_model = settings.normalize_trial_model(
        "claude-code", request.model or settings.analysis_model
    )
    prompt_sha256 = hashlib.sha256(request.prompt_text.encode("utf-8")).hexdigest()
    experiment = ExperimentModel(
        name=request.name,
        org_id=org_id,
        owner_user_id=owner_user_id,
        is_collection=False,
        last_activity_at=utcnow(),
    )
    session.add(experiment)
    await session.flush()

    created: list[QAEvalTrialResponse] = []
    for source_trial_id in request.source_trial_ids:
        source = source_by_id[source_trial_id]
        task_version_id = source.task_version_id
        assert task_version_id is not None
        task = task_by_id[source.task_id]
        version = version_by_id[task_version_id]
        pre_trial_items = (
            (version.pre_trial or {}).get("items")
            if isinstance(version.pre_trial, dict)
            else None
        )
        trial = await create_analysis_trial(
            session,
            task=task,
            kind="qa_eval",
            brief=build_qa_brief(
                task_name=task.name,
                trial_ids=[source.id],
                pre_trial_items=pre_trial_items,
                with_verdict=False,
                classification_prompt=request.prompt_text,
            ),
            task_version_id=task_version_id,
            experiment_id=experiment.id,
            model=canonical_model,
            billed_user_id=billed_user_id,
            payload={
                "trial_ids": [source.id],
                "with_verdict": False,
                "prompt_name": request.prompt_name,
                "prompt_sha256": prompt_sha256,
            },
        )
        created.append(
            QAEvalTrialResponse(source_trial_id=source.id, qa_eval_trial_id=trial.id)
        )

    response = QAEvalCreateResponse(
        experiment_id=experiment.id,
        experiment_name=experiment.name,
        prompt_name=request.prompt_name,
        prompt_sha256=prompt_sha256,
        model=canonical_model,
        trials=created,
    )
    if reservation is not None and idempotency_store is not None and org_id is not None:
        await session.flush()
        await idempotency_store.complete(
            org_id,
            _QA_EVAL_ROUTE,
            reservation.key_hash,
            response.model_dump(mode="json"),
        )
    return response

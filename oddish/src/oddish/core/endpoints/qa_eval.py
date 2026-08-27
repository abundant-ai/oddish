"""Create isolated QA prompt replays over historical solver evidence."""

from __future__ import annotations

import hashlib

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import is_nop_oracle_agent, settings
from oddish.db import (
    ACTIVE_TRIAL_STATUSES,
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
    QAEvalResultRow,
    QAEvalResultsResponse,
    QAEvalTrialResponse,
)
from oddish.workers.analysis_trials import (
    build_qa_eval_brief,
    create_analysis_trial,
)

_QA_EVAL_SOURCE_STATUSES = (TrialStatus.SUCCESS, TrialStatus.FAILED)


async def create_qa_eval_core(
    session: AsyncSession,
    *,
    request: QAEvalCreateRequest,
    org_id: str | None,
    owner_user_id: str | None,
) -> QAEvalCreateResponse:
    """Queue one candidate-prompt replay for each exact source trial ID."""
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
    missing = [
        trial_id
        for trial_id in request.source_trial_ids
        if trial_id not in source_by_id
    ]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Source trials not found in organization: {', '.join(missing)}",
        )

    ordered_sources = [source_by_id[trial_id] for trial_id in request.source_trial_ids]
    task_ids = list(dict.fromkeys(row.task_id for row in ordered_sources))
    version_ids = list(
        dict.fromkeys(
            row.task_version_id for row in ordered_sources if row.task_version_id
        )
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

    for source in ordered_sources:
        reason: str | None = None
        if source.kind != "agent" or source.is_probe or is_nop_oracle_agent(source.agent):
            reason = "is not an ordinary solver trial"
        elif source.superseded_by_trial_id is not None:
            reason = "has been superseded"
        elif source.status not in _QA_EVAL_SOURCE_STATUSES:
            reason = f"is not terminal (status={source.status.value})"
        elif source.task_version_id is None:
            reason = "has no exact task-version ID"
        elif source.task_id not in task_by_id:
            reason = "has no live task in the caller's organization"
        elif source.task_version_id not in version_by_id:
            reason = "has no live task-version row"
        elif version_by_id[source.task_version_id].task_id != source.task_id:
            reason = "references a task version owned by another task"
        elif not version_by_id[source.task_version_id].task_s3_key:
            reason = "has no stored exact task-version files"
        elif not source.has_trajectory:
            reason = "has no stored trajectory"
        elif not isinstance(source.analysis, dict) or not source.analysis.get(
            "classification"
        ):
            reason = "has no historical QA analysis for comparison"
        if reason is not None:
            raise HTTPException(
                status_code=409, detail=f"Source trial {source.id} {reason}"
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

    # A QA-eval experiment owns its replay trials through
    # ``TrialModel.experiment_id``. Do not add ``task_experiments`` rows here:
    # that relationship represents ordinary experiment membership and its
    # unordered first non-shadow member is still used as the fallback target
    # when later solver trials are appended to a task.
    created: list[QAEvalTrialResponse] = []
    for source in ordered_sources:
        task = task_by_id[source.task_id]
        version = version_by_id[source.task_version_id]
        pre_trial_items = (
            (version.pre_trial or {}).get("items")
            if isinstance(version.pre_trial, dict)
            else None
        )
        trial = await create_analysis_trial(
            session,
            task=task,
            kind="qa_eval",
            brief=build_qa_eval_brief(
                task_name=task.name,
                source_trial_id=source.id,
                candidate_prompt=request.prompt_text,
                pre_trial_items=pre_trial_items,
            ),
            task_version_id=source.task_version_id,
            experiment_id=experiment.id,
            model=canonical_model,
            payload={
                "source_trial_id": source.id,
                "source_task_version_id": source.task_version_id,
                "prompt_name": request.prompt_name,
                "prompt_sha256": prompt_sha256,
                "model": canonical_model,
            },
        )
        created.append(
            QAEvalTrialResponse(
                source_trial_id=source.id, qa_eval_trial_id=trial.id
            )
        )

    return QAEvalCreateResponse(
        experiment_id=experiment.id,
        experiment_name=experiment.name,
        prompt_name=request.prompt_name,
        prompt_sha256=prompt_sha256,
        model=canonical_model,
        trials=created,
    )


async def get_qa_eval_results_core(
    session: AsyncSession,
    *,
    experiment_ref: str,
    org_id: str | None,
) -> QAEvalResultsResponse:
    """Return the historical/candidate comparison rows for one replay."""
    experiment = await session.scalar(
        select(ExperimentModel).where(
            ExperimentModel.id == experiment_ref, ExperimentModel.org_id == org_id
        )
    )
    if experiment is None:
        experiment = await session.scalar(
            select(ExperimentModel)
            .join(TrialModel, TrialModel.experiment_id == ExperimentModel.id)
            .where(
                ExperimentModel.name == experiment_ref,
                ExperimentModel.org_id == org_id,
                ExperimentModel.is_collection.isnot(True),
                ExperimentModel.shadow_of.is_(None),
                TrialModel.kind == "qa_eval",
                TrialModel.org_id == org_id,
            )
            .order_by(ExperimentModel.created_at.desc())
            .limit(1)
        )
    if experiment is None:
        raise HTTPException(status_code=404, detail="QA evaluation not found")

    eval_trials = (
        (
            await session.execute(
                select(TrialModel)
                .where(
                    TrialModel.experiment_id == experiment.id,
                    TrialModel.org_id == org_id,
                    TrialModel.kind == "qa_eval",
                    TrialModel.superseded_by_trial_id.is_(None),
                )
                .order_by(TrialModel.created_at, TrialModel.id)
            )
        )
        .scalars()
        .all()
    )
    if not eval_trials:
        raise HTTPException(
            status_code=409,
            detail=f"Experiment {experiment.id} contains no QA-eval trials",
        )

    source_ids = [
        str(
            ((trial.harbor_config or {}).get("analysis_payload") or {}).get(
                "source_trial_id"
            )
            or ""
        )
        for trial in eval_trials
    ]
    source_rows = (
        await session.execute(
            select(TrialModel, TaskModel.name)
            .join(TaskModel, TaskModel.id == TrialModel.task_id)
            .where(TrialModel.id.in_(source_ids), TrialModel.org_id == org_id)
            .execution_options(include_deleted=True)
        )
    ).all()
    source_by_id = {source.id: (source, task_name) for source, task_name in source_rows}

    rows: list[QAEvalResultRow] = []
    for eval_trial, source_id in zip(eval_trials, source_ids, strict=True):
        source_pair = source_by_id.get(source_id)
        source = source_pair[0] if source_pair else None
        historical = source.analysis if source and isinstance(source.analysis, dict) else {}
        candidate = eval_trial.analysis if isinstance(eval_trial.analysis, dict) else {}
        valid = bool(candidate)
        if valid:
            failure_stage = None
        elif eval_trial.status in ACTIVE_TRIAL_STATUSES:
            failure_stage = eval_trial.status.value
        elif eval_trial.status != TrialStatus.SUCCESS:
            failure_stage = eval_trial.harbor_stage or "trial_execution"
        else:
            failure_stage = "qa_eval_import"
        rows.append(
            QAEvalResultRow(
                source_trial_id=source_id,
                qa_eval_trial_id=eval_trial.id,
                task_name=source_pair[1] if source_pair else eval_trial.task_id,
                status=eval_trial.status,
                historical_qa_classification=historical.get("classification"),
                historical_qa_root_cause=historical.get("root_cause"),
                candidate_qa_classification=candidate.get("classification"),
                candidate_qa_root_cause=candidate.get("root_cause"),
                qa_response_valid=valid,
                failure_stage=failure_stage,
            )
        )

    return QAEvalResultsResponse(
        experiment_id=experiment.id,
        experiment_name=experiment.name,
        rows=rows,
    )

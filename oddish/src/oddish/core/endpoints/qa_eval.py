"""Create QA prompt replays that point at historical solver trials."""

from __future__ import annotations

import hashlib
import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import is_nop_oracle_agent, settings
from oddish.core.analysis_payload import AnalysisPayloadError, parse_analysis_payload
from oddish.core.helpers import build_compact_trial_response
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
    QAEvalExperimentResponse,
    QAEvalExperimentTrialResponse,
    QAEvalTrialResponse,
)
from oddish.workers.analysis_trials import (
    build_qa_brief,
    create_analysis_trial,
    pre_trial_item_ids,
    qa_trial_evidence,
)

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
    for source_index, source_trial_id in enumerate(request.source_trial_ids, start=1):
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
        evidence = [qa_trial_evidence(source)]
        item_ids, must_fix_ids = pre_trial_item_ids(pre_trial_items)
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
                trial_evidence=evidence,
                pre_trial_status=(
                    version.pre_trial_status.value
                    if version.pre_trial_status is not None
                    else None
                ),
                pre_trial_error=version.pre_trial_error,
                verdict_omission_reason="QA replay does not synthesize a task verdict",
            ),
            task_version_id=task_version_id,
            experiment_id=experiment.id,
            model=canonical_model,
            billed_user_id=billed_user_id,
            payload={
                "trial_ids": [source.id],
                "trial_evidence": evidence,
                "baseline_evidence": [],
                "pre_trial_item_ids": item_ids,
                "pre_trial_must_fix_ids": must_fix_ids,
                "with_verdict": False,
                "prompt_name": request.prompt_name,
                "prompt_sha256": prompt_sha256,
                "source_index": source_index,
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


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _qa_eval_row_metadata(
    trial: TrialModel,
) -> tuple[int | None, str | None, str | None, str | None, str | None]:
    """Read display metadata without letting a malformed stored row hide itself."""
    payload = (trial.harbor_config or {}).get("analysis_payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    errors: list[str] = []

    source_trial_id: str | None = None
    try:
        parsed = parse_analysis_payload("qa_eval", trial.harbor_config)
        source_trial_id = parsed.trial_ids[0]
    except AnalysisPayloadError as exc:
        errors.append(str(exc))
        raw_ids = payload_dict.get("trial_ids")
        if (
            isinstance(raw_ids, list)
            and len(raw_ids) == 1
            and isinstance(raw_ids[0], str)
            and raw_ids[0].strip()
        ):
            source_trial_id = raw_ids[0].strip()

    source_index_value = payload_dict.get("source_index")
    source_index = (
        source_index_value
        if isinstance(source_index_value, int)
        and not isinstance(source_index_value, bool)
        and source_index_value >= 1
        else None
    )
    if source_index_value is not None and source_index is None:
        errors.append(
            "qa_eval analysis_payload.source_index must be a positive integer"
        )

    prompt_name_value = payload_dict.get("prompt_name")
    prompt_name = (
        prompt_name_value.strip()
        if isinstance(prompt_name_value, str) and prompt_name_value.strip()
        else None
    )
    if prompt_name is None:
        errors.append("qa_eval analysis_payload.prompt_name must be a non-empty string")

    prompt_sha256_value = payload_dict.get("prompt_sha256")
    prompt_sha256 = (
        prompt_sha256_value.lower()
        if isinstance(prompt_sha256_value, str)
        and _SHA256_PATTERN.fullmatch(prompt_sha256_value.lower())
        else None
    )
    if prompt_sha256 is None:
        errors.append(
            "qa_eval analysis_payload.prompt_sha256 must be 64 hex characters"
        )

    return (
        source_index,
        source_trial_id,
        prompt_name,
        prompt_sha256,
        "; ".join(dict.fromkeys(errors)) or None,
    )


def _source_import_provenance(
    harbor_config: dict | None,
) -> tuple[str | None, str | None]:
    imported_source = (harbor_config or {}).get("imported_source")
    if not isinstance(imported_source, dict):
        return None, None
    case_name_value = imported_source.get("golden_case")
    production_trial_id_value = imported_source.get("trial_id")
    case_name = (
        case_name_value.strip()
        if isinstance(case_name_value, str) and case_name_value.strip()
        else None
    )
    production_trial_id = (
        production_trial_id_value.strip()
        if isinstance(production_trial_id_value, str)
        and production_trial_id_value.strip()
        else None
    )
    return case_name, production_trial_id


async def get_qa_eval_experiment_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None,
) -> QAEvalExperimentResponse:
    """Return the private UI read model for one QA replay experiment."""
    experiment = await session.scalar(
        select(ExperimentModel).where(
            ExperimentModel.id == experiment_id,
            ExperimentModel.org_id == org_id,
        )
    )
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")

    rows = (
        await session.execute(
            select(TrialModel, TaskModel.task_path, TaskModel.name)
            .join(TaskModel, TaskModel.id == TrialModel.task_id)
            .where(
                TrialModel.experiment_id == experiment.id,
                TrialModel.org_id == org_id,
                TrialModel.kind == "qa_eval",
                TrialModel.superseded_by_trial_id.is_(None),
                TaskModel.org_id == org_id,
            )
            .order_by(TrialModel.created_at.asc(), TrialModel.id.asc())
        )
    ).all()

    parsed_rows: list[tuple] = []
    source_trial_ids: set[str] = set()
    for fallback_index, (trial, task_path, task_name) in enumerate(rows, start=1):
        (
            source_index,
            source_trial_id,
            prompt_name,
            prompt_sha256,
            stored_payload_error,
        ) = _qa_eval_row_metadata(trial)
        if source_trial_id is not None:
            source_trial_ids.add(source_trial_id)
        parsed_rows.append(
            (
                fallback_index,
                trial,
                task_path,
                task_name,
                source_index,
                source_trial_id,
                prompt_name,
                prompt_sha256,
                stored_payload_error,
            )
        )

    source_provenance: dict[str, tuple[str | None, str | None]] = {}
    if source_trial_ids:
        source_rows = (
            await session.execute(
                select(TrialModel.id, TrialModel.harbor_config).where(
                    TrialModel.id.in_(source_trial_ids),
                    TrialModel.org_id == org_id,
                )
            )
        ).all()
        source_provenance = {
            source_trial_id: _source_import_provenance(harbor_config)
            for source_trial_id, harbor_config in source_rows
        }

    indexed_entries: list[tuple[int, QAEvalExperimentTrialResponse]] = []
    for (
        fallback_index,
        trial,
        task_path,
        task_name,
        source_index,
        source_trial_id,
        prompt_name,
        prompt_sha256,
        stored_payload_error,
    ) in parsed_rows:
        source_case_name, production_trial_id = source_provenance.get(
            source_trial_id or "", (None, None)
        )
        indexed_entries.append(
            (
                fallback_index,
                QAEvalExperimentTrialResponse(
                    source_index=source_index,
                    source_trial_id=source_trial_id,
                    source_task_id=trial.task_id,
                    source_task_name=task_name,
                    source_case_name=source_case_name,
                    production_trial_id=production_trial_id,
                    prompt_name=prompt_name,
                    prompt_sha256=prompt_sha256,
                    stored_payload_error=stored_payload_error,
                    trial=build_compact_trial_response(trial, task_path),
                ),
            )
        )
    indexed_entries.sort(
        key=lambda item: (
            item[1].source_index is None,
            item[1].source_index or item[0],
            item[0],
        )
    )
    entries = [entry for _, entry in indexed_entries]

    return QAEvalExperimentResponse(
        experiment_id=experiment.id,
        name=experiment.name,
        created_at=experiment.created_at,
        is_qa_eval=bool(entries),
        prompt_names=sorted(
            {entry.prompt_name for entry in entries if entry.prompt_name is not None}
        ),
        prompt_sha256s=sorted(
            {
                entry.prompt_sha256
                for entry in entries
                if entry.prompt_sha256 is not None
            }
        ),
        models=sorted(
            {entry.trial.model for entry in entries if entry.trial.model is not None}
        ),
        trials=entries,
    )

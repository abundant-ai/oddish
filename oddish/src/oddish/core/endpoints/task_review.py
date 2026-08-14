"""Canonical, bounded, read-only QA review for one immutable task version."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, case, func, not_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.analyze.models import (
    ActionItem,
    ActionTier,
    ExploitationAssessment,
    compute_action_item_id,
)
from oddish.config import nop_oracle_kind
from oddish.core.baseline_gate import (
    baseline_agent_clause,
    baseline_agent_kind_clauses,
)
from oddish.core.qa_scope import (
    analysis_fingerprint,
    input_set_sha256,
    qa_review_scope,
)
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskQaRunDisposition,
    TaskQaRunModel,
    TaskVersionModel,
    TrialModel,
    VerdictStatus,
    WorkerJobModel,
    WorkerJobStatus,
)
from oddish.schemas import (
    TaskQaRunProvenance,
    TaskReviewBaselineResult,
    TaskReviewBaselines,
    TaskReviewClassificationCounts,
    TaskReviewFinding,
    TaskReviewFindingCounts,
    TaskReviewFindingsPage,
    TaskReviewQa,
    TaskReviewResponse,
    TaskReviewScope,
    TaskReviewTask,
    TaskReviewTrial,
    TaskReviewTrialCounts,
    TaskReviewTrialsPage,
    TaskReviewVerdict,
)

_TIERS = (
    ActionTier.MUST_FIX,
    ActionTier.SHOULD_FIX,
    ActionTier.OPTIONAL,
)
_TIER_RANK = {tier: rank for rank, tier in enumerate(_TIERS)}
_CLASSIFICATION_RANK = {
    "BAD_SUCCESS": 0,
    "BAD_FAILURE": 1,
    "HARNESS_ERROR": 2,
    "GOOD_FAILURE": 3,
    "GOOD_SUCCESS": 4,
}
_ACTIVE_WORKER_STATUSES = {
    WorkerJobStatus.QUEUED,
    WorkerJobStatus.RETRYING,
    WorkerJobStatus.RUNNING,
    WorkerJobStatus.BLOCKED,
}
_SECRET_KEY_PARTS = (
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "access_key",
    "private_key",
)


def _cursor(kind: str, values: list[Any]) -> str:
    payload = json.dumps(
        {"v": 1, "kind": kind, "after": values},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str | None, *, kind: str, size: int) -> list[Any] | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        after = payload["after"]
        if payload != {"v": 1, "kind": kind, "after": after}:
            raise ValueError
        if not isinstance(after, list) or len(after) != size:
            raise ValueError
        return after
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(status_code=422, detail=f"Invalid {kind} cursor") from None


def _finding_key(finding: TaskReviewFinding) -> tuple[int, str, int, str]:
    return (
        _TIER_RANK[finding.tier],
        finding.file,
        finding.line_start,
        finding.id,
    )


def _action_item(payload: Any) -> ActionItem:
    item = ActionItem.model_validate(payload)
    item.id = item.id or compute_action_item_id(item)
    return item


def _merge_findings(
    pre_trial: dict | None,
    trial_fragments: list[Any],
) -> list[TaskReviewFinding]:
    """Perform the review contract's sole semantic finding merge."""

    merged: dict[str, dict[str, Any]] = {}
    for payload in (pre_trial or {}).get("items", []):
        item = _action_item(payload)
        # The legacy aggregation wrote task-wide exploitation stamps back into
        # pre_trial. Rebuild linkage from this endpoint's exact version/scope.
        item.exploited = False
        item.exploit_evidence = None
        item.causal = False
        item.links_to = None
        merged[item.id] = {
            "item": item,
            "from_pre_trial": True,
            "trial_ids": set(),
            "experiment_ids": set(),
        }

    for row in sorted(trial_fragments, key=lambda candidate: candidate.id):
        trial_id = row.id
        experiment_id = row.experiment_id
        for payload in row.action_items or []:
            item = _action_item(payload)
            target = merged.get(item.links_to or "")
            if target is not None and target["from_pre_trial"]:
                target["trial_ids"].add(trial_id)
                target["experiment_ids"].add(experiment_id)
                target_item: ActionItem = target["item"]
                target_item.exploited = target_item.exploited or item.exploited
                target_item.causal = target_item.causal or item.causal
                if item.exploited and item.exploit_evidence:
                    target_item.exploit_evidence = (
                        target_item.exploit_evidence or item.exploit_evidence
                    )
                continue

            existing = merged.get(item.id)
            if existing is None:
                existing = merged[item.id] = {
                    "item": item,
                    "from_pre_trial": False,
                    "trial_ids": set(),
                    "experiment_ids": set(),
                }
            existing["trial_ids"].add(trial_id)
            existing["experiment_ids"].add(experiment_id)
            existing_item: ActionItem = existing["item"]
            existing_item.exploited = existing_item.exploited or item.exploited
            existing_item.causal = existing_item.causal or item.causal
            if item.exploited and item.exploit_evidence:
                existing_item.exploit_evidence = (
                    existing_item.exploit_evidence or item.exploit_evidence
                )

        for payload in row.exploitation or []:
            assessment = ExploitationAssessment.model_validate(payload)
            if not assessment.exploited:
                continue
            target = merged.get(assessment.links_to)
            if target is None or not target["from_pre_trial"]:
                continue
            target["trial_ids"].add(trial_id)
            target["experiment_ids"].add(experiment_id)
            target_item = target["item"]
            target_item.exploited = True
            target_item.causal = target_item.causal or assessment.causal
            if assessment.exploit_evidence:
                target_item.exploit_evidence = (
                    target_item.exploit_evidence or assessment.exploit_evidence
                )

    findings = [
        TaskReviewFinding(
            **entry["item"].model_dump(mode="python", exclude={"id"}),
            id=finding_id,
            from_pre_trial=entry["from_pre_trial"],
            trial_ids=sorted(entry["trial_ids"]),
            experiment_ids=sorted(entry["experiment_ids"]),
        )
        for finding_id, entry in merged.items()
    ]
    findings.sort(key=_finding_key)
    return findings


def _sanitized_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitized_config(child)
            for key, child in sorted(value.items())
            if not any(part in key.lower() for part in _SECRET_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_sanitized_config(child) for child in value]
    return value


def _config_fingerprint(row: Any) -> str:
    payload = {
        "agent": row.agent,
        "model": row.model,
        "environment": row.environment,
        "harbor_sha": row.harbor_sha,
        "harbor_config": _sanitized_config(row.harbor_config),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _disposition(value: Any) -> TaskQaRunDisposition | None:
    if value is None or isinstance(value, TaskQaRunDisposition):
        return value
    return TaskQaRunDisposition(value)


def _worker_status(value: Any) -> WorkerJobStatus:
    if isinstance(value, WorkerJobStatus):
        return value
    return WorkerJobStatus(value)


def _qa_status_for_worker(value: Any) -> VerdictStatus:
    status = _worker_status(value)
    if status == WorkerJobStatus.RUNNING:
        return VerdictStatus.RUNNING
    if status == WorkerJobStatus.RETRYING:
        return VerdictStatus.RETRYING
    return VerdictStatus.QUEUED


def _run_provenance(
    row: Any,
    current_fingerprints: dict[str, str],
) -> TaskQaRunProvenance:
    input_ids = list(row.input_trial_ids or [])
    stored = dict(row.input_analysis_fingerprints or {})
    changed_count = 0
    if stored:
        changed_count = sum(
            1
            for trial_id in input_ids
            if stored.get(trial_id) != current_fingerprints.get(trial_id)
        )
    return TaskQaRunProvenance(
        id=row.id,
        disposition=_disposition(row.disposition),
        task_version_id=row.task_version_id,
        worker_job_id=row.worker_job_id,
        input_trial_count=len(input_ids),
        input_set_sha256=input_set_sha256(stored),
        input_analysis_changed_count=changed_count,
        pre_trial_block_id=row.pre_trial_block_id,
        verdict_block_id=row.verdict_block_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


async def get_task_review_core(
    session: AsyncSession,
    *,
    task_ref: str,
    org_id: str | None = None,
    version: int | None = None,
    experiment_id: str | None = None,
    tiers: list[ActionTier] | None = None,
    finding_limit: int = 20,
    finding_cursor: str | None = None,
    trial_limit: int = 20,
    trial_cursor: str | None = None,
) -> TaskReviewResponse:
    """Read one version's merged QA evidence without enqueueing or writing."""

    if not 0 <= finding_limit <= 20 or not 0 <= trial_limit <= 20:
        raise HTTPException(status_code=422, detail="Review limits must be between 0 and 20")

    selected_tiers = [tier for tier in _TIERS if tiers is None or tier in tiers]
    if tiers is not None and not selected_tiers:
        selected_tiers = []

    task_query = select(
        TaskModel.id.label("id"),
        TaskModel.name.label("name"),
        TaskModel.current_version_id.label("current_version_id"),
        TaskModel.verdict.label("task_verdict"),
        TaskModel.verdict_status.label("task_verdict_status"),
        TaskModel.published_qa_run_id.label("published_qa_run_id"),
        TaskModel.verdict_version_id.label("verdict_version_id"),
    ).where(or_(TaskModel.id == task_ref, TaskModel.name == task_ref))
    if org_id is not None:
        task_query = task_query.where(TaskModel.org_id == org_id)
    task_query = task_query.order_by(
        case((TaskModel.id == task_ref, 0), else_=1), TaskModel.id
    ).limit(1)
    task = (await session.execute(task_query)).one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_ref} not found")

    if version is None and task.current_version_id is None:
        raise HTTPException(
            status_code=404, detail=f"Task {task.id} has no selected version"
        )
    version_query = select(
        TaskVersionModel.id.label("id"),
        TaskVersionModel.version.label("version"),
        TaskVersionModel.content_hash.label("content_hash"),
        TaskVersionModel.pre_trial.label("pre_trial"),
    ).where(TaskVersionModel.task_id == task.id)
    if version is None:
        version_query = version_query.where(
            TaskVersionModel.id == task.current_version_id
        )
    else:
        version_query = version_query.where(TaskVersionModel.version == version)
    version_row = (await session.execute(version_query)).one_or_none()
    if version_row is None:
        requested = f"version {version}" if version is not None else "selected version"
        raise HTTPException(
            status_code=404, detail=f"Task {task.id} {requested} not found"
        )

    run_rows = (
        await session.execute(
            select(
                TaskQaRunModel.id.label("id"),
                TaskQaRunModel.disposition.label("disposition"),
                TaskQaRunModel.task_version_id.label("task_version_id"),
                TaskQaRunModel.worker_job_id.label("worker_job_id"),
                TaskQaRunModel.input_trial_ids.label("input_trial_ids"),
                TaskQaRunModel.input_analysis_fingerprints.label(
                    "input_analysis_fingerprints"
                ),
                TaskQaRunModel.verdict.label("verdict"),
                TaskQaRunModel.pre_trial_block_id.label("pre_trial_block_id"),
                TaskQaRunModel.verdict_block_id.label("verdict_block_id"),
                TaskQaRunModel.started_at.label("started_at"),
                TaskQaRunModel.finished_at.label("finished_at"),
                TaskQaRunModel.created_at.label("created_at"),
                WorkerJobModel.status.label("worker_status"),
            )
            .join(WorkerJobModel, WorkerJobModel.id == TaskQaRunModel.worker_job_id)
            .where(
                TaskQaRunModel.task_id == task.id,
                TaskQaRunModel.task_version_id == version_row.id,
                TaskQaRunModel.deleted_at.is_(None),
            )
            .order_by(TaskQaRunModel.created_at, TaskQaRunModel.id)
        )
    ).all()

    result_run = None
    active_run = None
    effective_status: VerdictStatus | None = None
    for run in run_rows:
        disposition = _disposition(run.disposition)
        worker_status = _worker_status(run.worker_status)
        if disposition == TaskQaRunDisposition.PUBLISHED:
            result_run = run
            effective_status = VerdictStatus.SUCCESS
        elif disposition == TaskQaRunDisposition.FAILED:
            result_run = None
            effective_status = VerdictStatus.FAILED
        if disposition is None and worker_status in _ACTIVE_WORKER_STATUSES:
            active_run = run

    fingerprint_ids = {
        trial_id
        for run in (result_run, active_run)
        if run is not None
        for trial_id in (run.input_trial_ids or [])
    }
    current_fingerprints: dict[str, str] = {}
    if fingerprint_ids:
        current_rows = await session.execute(
            select(TrialModel.id, TrialModel.analysis).where(
                TrialModel.id.in_(fingerprint_ids)
            )
        )
        current_fingerprints = {
            trial_id: analysis_fingerprint(analysis)
            for trial_id, analysis in current_rows.all()
        }

    result_provenance = (
        _run_provenance(result_run, current_fingerprints)
        if result_run is not None
        else None
    )
    active_provenance = (
        _run_provenance(active_run, current_fingerprints)
        if active_run is not None
        else None
    )
    qa_status = (
        _qa_status_for_worker(active_run.worker_status)
        if active_run is not None
        else effective_status
    )

    nop_clause, oracle_clause = baseline_agent_kind_clauses(TrialModel.agent)
    baseline_clause = baseline_agent_clause(TrialModel.agent)
    model_clause = not_(baseline_clause)
    classification = TrialModel.analysis["classification"].astext
    review_where = [qa_review_scope(task.id, version_row.id)]
    if experiment_id is not None:
        review_where.append(TrialModel.experiment_id == experiment_id)

    counts = (
        await session.execute(
            select(
                func.count(TrialModel.id)
                .filter(model_clause)
                .label("model_count"),
                func.count(TrialModel.id)
                .filter(
                    and_(
                        model_clause,
                        TrialModel.analysis_status == AnalysisStatus.SUCCESS,
                    )
                )
                .label("analyzed_count"),
                *[
                    func.count(TrialModel.id)
                    .filter(
                        and_(
                            model_clause,
                            TrialModel.analysis_status == AnalysisStatus.SUCCESS,
                            classification == name,
                        )
                    )
                    .label(name.lower())
                    for name in _CLASSIFICATION_RANK
                ],
                func.count(TrialModel.id).filter(nop_clause).label("nop_count"),
                func.count(TrialModel.id)
                .filter(
                    and_(
                        nop_clause,
                        or_(TrialModel.reward.is_(None), TrialModel.reward != 0),
                    )
                )
                .label("nop_unexpected"),
                func.count(TrialModel.id)
                .filter(oracle_clause)
                .label("oracle_count"),
                func.count(TrialModel.id)
                .filter(
                    and_(
                        oracle_clause,
                        or_(TrialModel.reward.is_(None), TrialModel.reward != 1),
                    )
                )
                .label("oracle_unexpected"),
            ).where(*review_where)
        )
    ).one()

    nop_valid = counts.nop_count > 0 and counts.nop_unexpected == 0
    oracle_valid = counts.oracle_count > 0 and counts.oracle_unexpected == 0
    baselines = TaskReviewBaselines(
        outcome="valid" if nop_valid and oracle_valid else "faulty",
        nop=TaskReviewBaselineResult(
            expected_reward=0,
            valid=nop_valid,
            trial_count=counts.nop_count,
            unexpected_count=counts.nop_unexpected,
        ),
        oracle=TaskReviewBaselineResult(
            expected_reward=1,
            valid=oracle_valid,
            trial_count=counts.oracle_count,
            unexpected_count=counts.oracle_unexpected,
        ),
    )

    fragment_rows = (
        await session.execute(
            select(
                TrialModel.id.label("id"),
                TrialModel.experiment_id.label("experiment_id"),
                TrialModel.analysis["action_items"].label("action_items"),
                TrialModel.analysis["exploitation"].label("exploitation"),
            )
            .where(
                *review_where,
                model_clause,
                TrialModel.analysis_status == AnalysisStatus.SUCCESS,
                TrialModel.analysis.isnot(None),
            )
            .order_by(TrialModel.id)
        )
    ).all()
    findings = _merge_findings(version_row.pre_trial, fragment_rows)
    tier_counts = {
        tier: sum(1 for finding in findings if finding.tier == tier) for tier in _TIERS
    }
    filtered_findings = [
        finding for finding in findings if finding.tier in selected_tiers
    ]

    finding_after = _decode_cursor(finding_cursor, kind="finding", size=4)
    if finding_after is not None:
        try:
            finding_after_key = (
                int(finding_after[0]),
                str(finding_after[1]),
                int(finding_after[2]),
                str(finding_after[3]),
            )
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Invalid finding cursor") from None
        filtered_findings = [
            finding
            for finding in filtered_findings
            if _finding_key(finding) > finding_after_key
        ]
    if finding_limit == 0:
        finding_page_items: list[TaskReviewFinding] = []
        findings_has_more = False
    else:
        finding_page_items = filtered_findings[:finding_limit]
        findings_has_more = len(filtered_findings) > finding_limit
    next_finding_cursor = (
        _cursor("finding", list(_finding_key(finding_page_items[-1])))
        if findings_has_more and finding_page_items
        else None
    )

    role_order = case((nop_clause, 0), (oracle_clause, 1), else_=2)
    class_order = case(
        *[
            (classification == name, rank)
            for name, rank in _CLASSIFICATION_RANK.items()
        ],
        else_=5,
    )
    trial_query = select(
        TrialModel.id.label("id"),
        TrialModel.experiment_id.label("experiment_id"),
        TrialModel.agent.label("agent"),
        TrialModel.model.label("model"),
        TrialModel.environment.label("environment"),
        TrialModel.harbor_sha.label("harbor_sha"),
        TrialModel.harbor_config.label("harbor_config"),
        TrialModel.status.label("status"),
        TrialModel.reward.label("reward"),
        TrialModel.cost_usd.label("cost_usd"),
        TrialModel.trajectory_duration_seconds.label("duration_seconds"),
        TrialModel.analysis_status.label("analysis_status"),
        TrialModel.analysis.label("analysis"),
        TrialModel.created_at.label("created_at"),
        role_order.label("role_order"),
        class_order.label("class_order"),
    ).where(*review_where)
    trial_after = _decode_cursor(trial_cursor, kind="trial", size=4)
    if trial_after is not None:
        try:
            after_created_at = datetime.fromisoformat(str(trial_after[2]))
            after_values = (
                int(trial_after[0]),
                int(trial_after[1]),
                after_created_at,
                str(trial_after[3]),
            )
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Invalid trial cursor") from None
        trial_query = trial_query.where(
            tuple_(role_order, class_order, TrialModel.created_at, TrialModel.id)
            > tuple_(*after_values)
        )
    trial_rows = []
    if trial_limit:
        trial_rows = (
            await session.execute(
                trial_query.order_by(
                    role_order, class_order, TrialModel.created_at, TrialModel.id
                ).limit(trial_limit + 1)
            )
        ).all()
    trials_has_more = len(trial_rows) > trial_limit
    trial_rows = trial_rows[:trial_limit]

    result_fingerprints = (
        dict(result_run.input_analysis_fingerprints or {})
        if result_run is not None
        else {}
    )
    result_input_ids = (
        set(result_run.input_trial_ids or []) if result_run is not None else set()
    )
    trial_items = []
    for row in trial_rows:
        role = nop_oracle_kind(row.agent) or "model"
        expected_fingerprint = result_fingerprints.get(row.id)
        trial_items.append(
            TaskReviewTrial(
                id=row.id,
                role=role,
                experiment_id=row.experiment_id,
                agent=row.agent,
                model=row.model,
                config_fingerprint=_config_fingerprint(row),
                environment=row.environment,
                harbor_sha=row.harbor_sha,
                status=row.status,
                reward=row.reward,
                cost_usd=row.cost_usd,
                duration_seconds=row.duration_seconds,
                included_in_result_run=row.id in result_input_ids,
                result_run_analysis_fingerprint=expected_fingerprint,
                analysis_matches_result_run=(
                    analysis_fingerprint(row.analysis) == expected_fingerprint
                    if expected_fingerprint is not None
                    else None
                ),
                analysis_status=row.analysis_status if role == "model" else None,
                analysis=row.analysis if role == "model" else None,
            )
        )
    next_trial_cursor = (
        _cursor(
            "trial",
            [
                trial_rows[-1].role_order,
                trial_rows[-1].class_order,
                trial_rows[-1].created_at.isoformat(),
                trial_rows[-1].id,
            ],
        )
        if trials_has_more and trial_rows
        else None
    )

    verdict = (
        TaskReviewVerdict.model_validate(result_run.verdict)
        if result_run is not None and result_run.verdict is not None
        else None
    )
    classification_counts = TaskReviewClassificationCounts(
        GOOD_FAILURE=counts.good_failure,
        BAD_FAILURE=counts.bad_failure,
        GOOD_SUCCESS=counts.good_success,
        BAD_SUCCESS=counts.bad_success,
        HARNESS_ERROR=counts.harness_error,
    )
    return TaskReviewResponse(
        task=TaskReviewTask(
            id=task.id,
            name=task.name,
            version=version_row.version,
            version_id=version_row.id,
            content_hash=version_row.content_hash,
        ),
        scope=TaskReviewScope(
            experiment_id=experiment_id,
            tiers=selected_tiers,
            same_version_across_experiments=experiment_id is None,
        ),
        qa=TaskReviewQa(
            status=qa_status,
            result_run=result_provenance,
            active_run=active_provenance,
            is_task_published_run=(
                result_run is not None and task.published_qa_run_id == result_run.id
            ),
            legacy_unscoped_verdict_available=(
                task.task_verdict is not None and task.published_qa_run_id is None
            ),
            input_analysis_changed_after_run=(
                result_provenance is not None
                and result_provenance.input_analysis_changed_count > 0
            ),
        ),
        baselines=baselines,
        verdict=verdict,
        finding_counts=TaskReviewFindingCounts(
            unfiltered_total=len(findings),
            filtered_total=sum(tier_counts[tier] for tier in selected_tiers),
            must_fix=tier_counts[ActionTier.MUST_FIX],
            should_fix=tier_counts[ActionTier.SHOULD_FIX],
            optional=tier_counts[ActionTier.OPTIONAL],
        ),
        findings=finding_page_items,
        findings_page=TaskReviewFindingsPage(
            has_more=findings_has_more,
            next_cursor=next_finding_cursor,
        ),
        trial_counts=TaskReviewTrialCounts(
            eligible=counts.model_count,
            analyzed=counts.analyzed_count,
            unanalyzed=counts.model_count - counts.analyzed_count,
            classifications=classification_counts,
        ),
        trials=trial_items,
        trials_page=TaskReviewTrialsPage(
            has_more=trials_has_more,
            next_cursor=next_trial_cursor,
        ),
    )


__all__ = ["get_task_review_core"]

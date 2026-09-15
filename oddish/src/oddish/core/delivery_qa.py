"""Compare the latest QA run with the evidence currently selected for delivery."""

from collections import defaultdict
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from oddish.config import is_nop_oracle_agent
from oddish.core.analysis_payload import (
    AnalysisPayloadError,
    audit_snapshot_matches,
    parse_analysis_payload,
    qa_trial_evidence,
)
from oddish.db import (
    ACTIVE_TRIAL_STATUSES,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
)
from oddish.filters.trial_predicates import qa_eligible_trial_clauses
from oddish.schemas import DeliveryQAStatus
from oddish.verdict import verdict_label


async def delivery_qa_statuses(
    session: AsyncSession,
    *,
    tasks: dict[str, TaskModel],
    versions: dict[str, TaskVersionModel],
) -> dict[str, DeliveryQAStatus]:
    if not tasks:
        return {}
    # Instructions in harbor_config can dwarf the evidence. Project the payload
    # separately so an ORM instance never holds a truncated writable config.
    latest = (
        await session.execute(
            select(TrialModel, TrialModel.harbor_config["analysis_payload"])
            .options(
                load_only(
                    TrialModel.id,
                    TrialModel.task_id,
                    TrialModel.task_version_id,
                    TrialModel.status,
                    TrialModel.created_at,
                    TrialModel.finished_at,
                    TrialModel.error_message,
                    TrialModel.analysis_error,
                    raiseload=True,
                )
            )
            .where(
                TrialModel.task_id.in_(tasks),
                TrialModel.kind == "qa",
                TrialModel.superseded_by_trial_id.is_(None),
            )
            .distinct(TrialModel.task_id)
            .order_by(
                TrialModel.task_id, TrialModel.created_at.desc(), TrialModel.id.desc()
            )
        )
    ).all()
    # Fetch only bounded evidence fields, never trajectories or analyses, and
    # share admission's exclusions instead of approximating "eligible" here.
    evidence: dict[str, list[TrialModel]] = defaultdict(list)
    sources = (
        await session.execute(
            select(TrialModel, and_(*qa_eligible_trial_clauses()).label("eligible"))
            .options(
                load_only(
                    TrialModel.id,
                    TrialModel.task_version_id,
                    TrialModel.agent,
                    TrialModel.status,
                    TrialModel.reward,
                    TrialModel.has_trajectory,
                    TrialModel.finished_at,
                )
            )
            .where(
                TrialModel.task_version_id.in_(versions),
                TrialModel.kind == "agent",
                TrialModel.superseded_by_trial_id.is_(None),
            )
        )
    ).all()
    for source, eligible in sources:
        if eligible or is_nop_oracle_agent(source.agent):
            evidence[source.task_version_id].append(source)
    return {
        qa.task_id: evaluate_delivery_qa(
            task=tasks[qa.task_id],
            version=versions.get(tasks[qa.task_id].current_version_id),
            qa=qa,
            analysis_payload=analysis_payload,
            sources=evidence.get(tasks[qa.task_id].current_version_id, []),
        )
        for qa, analysis_payload in latest
    }


def evaluate_delivery_qa(
    *,
    task: TaskModel,
    version: TaskVersionModel | None,
    qa: TrialModel,
    analysis_payload: Any,
    sources: list[TrialModel],
) -> DeliveryQAStatus:
    result = DeliveryQAStatus(trial_id=qa.id, finished_at=qa.finished_at)
    if version is None or qa.task_version_id != version.id:
        result.status, result.detail = (
            "outdated",
            "No verdict",
        )
    elif qa.status in ACTIVE_TRIAL_STATUSES:
        result.status = (
            "running"
            if qa.status in {TrialStatus.RUNNING, TrialStatus.PAUSED}
            else "queued"
        )
        result.detail = (
            "Verdict running" if result.status == "running" else "Verdict queued"
        )
    elif qa.status != TrialStatus.SUCCESS or qa.analysis_error:
        result.status, result.detail = (
            "error",
            qa.error_message or qa.analysis_error or "No verdict",
        )
    elif qa.finished_at is None:
        result.status, result.detail = (
            "outdated",
            "Verdict completion time was not recorded",
        )
    else:
        try:
            payload = parse_analysis_payload(
                "qa", {"analysis_payload": analysis_payload}
            )
        except AnalysisPayloadError:
            result.status, result.detail = (
                "outdated",
                "Verdict evidence was not recorded; regenerate the verdict",
            )
            return result
        pinned = list(payload.trial_evidence + payload.baseline_evidence)
        current = [qa_trial_evidence(source) for source in sources]
        if (
            not payload.trial_evidence
            or sorted(pinned, key=lambda item: item["trial_id"])
            != sorted(current, key=lambda item: item["trial_id"])
            or any(
                source.finished_at is None or source.finished_at > qa.created_at
                for source in sources
            )
        ):
            result.status, result.detail = (
                "outdated",
                "Trials changed since verdict generation; regenerate the verdict",
            )
        elif not audit_snapshot_matches(version, analysis_payload):
            result.status, result.detail = (
                "outdated",
                "Pre-trial audit changed since verdict generation; regenerate the verdict",
            )
        elif (
            task.verdict_status != VerdictStatus.SUCCESS
            or not isinstance(task.verdict, dict)
            # Legacy synthesized verdicts lack a grader ID. A classification-only
            # run must explicitly own the verdict its baseline rules published.
            or task.verdict.get("_graded_by", qa.id if payload.with_verdict else None)
            != qa.id
        ):
            result.status, result.detail = "error", "No verdict"
        else:
            label = verdict_label(task.verdict_status, task.verdict)
            if label == "Accepted":
                result.status, result.detail = "accepted", label
            elif label == "Rejected":
                result.status, result.detail = (
                    "needs_fixes",
                    task.verdict.get("primary_issue") or label,
                )
            else:
                result.status, result.detail = "error", "No verdict"
    return result

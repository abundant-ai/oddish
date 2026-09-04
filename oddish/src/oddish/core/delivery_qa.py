"""Compare the latest QA run with the evidence currently selected for delivery."""

from collections import defaultdict

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


async def delivery_qa_statuses(
    session: AsyncSession,
    *,
    tasks: dict[str, TaskModel],
    versions: dict[str, TaskVersionModel],
) -> dict[str, DeliveryQAStatus]:
    if not tasks:
        return {}
    latest = (
        await session.scalars(
            select(TrialModel)
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
                    TrialModel.harbor_config,
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
            sources=evidence.get(tasks[qa.task_id].current_version_id, []),
        )
        for qa in latest
    }


def evaluate_delivery_qa(
    *,
    task: TaskModel,
    version: TaskVersionModel | None,
    qa: TrialModel,
    sources: list[TrialModel],
) -> DeliveryQAStatus:
    result = DeliveryQAStatus(trial_id=qa.id, finished_at=qa.finished_at)
    if version is None or qa.task_version_id != version.id:
        result.status, result.detail = "outdated", "QA covers a different task version"
    elif qa.status in ACTIVE_TRIAL_STATUSES:
        result.status = (
            "running"
            if qa.status in {TrialStatus.RUNNING, TrialStatus.PAUSED}
            else "queued"
        )
        result.detail = (
            "QA is running" if result.status == "running" else "QA is queued"
        )
    elif qa.status != TrialStatus.SUCCESS or qa.analysis_error:
        result.status, result.detail = (
            "error",
            qa.error_message or qa.analysis_error or "QA did not complete",
        )
    elif qa.finished_at is None:
        result.status, result.detail = "outdated", "QA completion time was not recorded"
    else:
        try:
            payload = parse_analysis_payload("qa", qa.harbor_config)
        except AnalysisPayloadError:
            result.status, result.detail = (
                "outdated",
                "QA evidence coverage was not recorded; rerun QA",
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
                "Trials changed since QA; rerun QA",
            )
        elif not audit_snapshot_matches(version, qa.harbor_config["analysis_payload"]):
            result.status, result.detail = (
                "outdated",
                "Source audit changed since QA; rerun QA",
            )
        elif (
            not payload.with_verdict
            or task.verdict_status != VerdictStatus.SUCCESS
            or not isinstance(task.verdict, dict)
        ):
            result.status, result.detail = "error", "QA produced no current verdict"
        elif task.verdict.get("is_good") is True:
            result.status, result.detail = (
                "accepted",
                "QA accepts the current version and trials",
            )
        else:
            result.status, result.detail = (
                "needs_fixes",
                task.verdict.get("primary_issue") or "QA rejects the current version",
            )
    return result

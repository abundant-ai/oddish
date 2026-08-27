from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, load_only

from oddish.core.baseline_gate import baseline_agent_clause
from oddish.core.cost_exclusions import load_cost_exclusions
from oddish.core.experiment_membership import visible_experiment_trial_predicates
from oddish.core.helpers import (
    SLIM_TRIAL_RESPONSE_COLUMNS,
    build_slim_trial_response,
    experiment_effective_versions_selectable,
)
from oddish.db import (
    ACTIVE_TRIAL_STATUSES,
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    task_experiments,
)
from oddish.schemas import (
    ExperimentOpenResponse,
    ExperimentPageSummary,
    ExperimentPageVerdict,
    ExperimentTaskRow,
    ExperimentTrialAnalysis,
    ExperimentTrialCell,
    ExperimentTrialPageResponse,
)

OPEN_MAX_TASKS = 100
OPEN_MAX_BYTES = 50_000
TRIAL_PAGE_MAX_TRIALS = 250
_ACTIVE_VERDICT_STATUSES = (
    VerdictStatus.PENDING,
    VerdictStatus.QUEUED,
    VerdictStatus.RUNNING,
)
_TRIAL_PAGE_COLUMNS = tuple(
    column
    for column in SLIM_TRIAL_RESPONSE_COLUMNS
    if column not in (TrialModel.analysis, TrialModel.error_message)
)


async def _member_experiment(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> Mapping[str, Any]:
    result = await session.execute(
        select(
            ExperimentModel.id,
            ExperimentModel.name,
            ExperimentModel.created_at,
            ExperimentModel.owner,
            ExperimentModel.link,
            func.coalesce(
                ExperimentModel.last_activity_at,
                ExperimentModel.updated_at,
                ExperimentModel.created_at,
            ).label("revision"),
        ).where(
            ExperimentModel.id == experiment_id,
            ExperimentModel.org_id == org_id,
            ExperimentModel.deleted_at.is_(None),
        )
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return row


def _experiment_task_rows(*, experiment_id: str, org_id: str):
    effective = experiment_effective_versions_selectable(experiment_id=experiment_id)
    scored = and_(
        TrialModel.status == TrialStatus.SUCCESS,
        TrialModel.reward.is_not(None),
        ~baseline_agent_clause(TrialModel.agent),
    )
    stats = (
        select(
            TrialModel.task_id.label("task_id"),
            func.count().label("total"),
            func.count()
            .filter(TrialModel.status == TrialStatus.SUCCESS)
            .label("completed"),
            func.count()
            .filter(TrialModel.status == TrialStatus.FAILED)
            .label("failed"),
            func.count()
            .filter(TrialModel.status == TrialStatus.SKIPPED)
            .label("skipped"),
            func.count()
            .filter(TrialModel.status == TrialStatus.SUCCESS, TrialModel.reward == 1)
            .label("pass_count"),
            func.count()
            .filter(
                TrialModel.status == TrialStatus.SUCCESS,
                TrialModel.reward.is_not(None),
                TrialModel.reward.not_in((0, 1)),
            )
            .label("partial_count"),
            func.count()
            .filter(TrialModel.status == TrialStatus.SUCCESS, TrialModel.reward == 0)
            .label("fail_count"),
            func.coalesce(
                func.sum(TrialModel.reward).filter(
                    TrialModel.status == TrialStatus.SUCCESS
                ),
                0.0,
            ).label("reward_sum"),
            func.count(TrialModel.reward)
            .filter(TrialModel.status == TrialStatus.SUCCESS)
            .label("reward_total"),
            func.avg(case((scored, TrialModel.reward))).label("average_score"),
        )
        .join(
            effective,
            and_(
                effective.c.task_id == TrialModel.task_id,
                effective.c.task_version_id == TrialModel.task_version_id,
            ),
        )
        .where(*visible_experiment_trial_predicates(experiment_id))
        .group_by(TrialModel.task_id)
        .subquery("experiment_task_stats")
    )
    current_version = aliased(TaskVersionModel)
    return (
        select(
            TaskModel.id.label("task_id"),
            TaskModel.name,
            TaskModel.status,
            TaskModel.priority,
            TaskModel.user,
            TaskModel.task_path,
            TaskModel.current_version_id,
            current_version.version.label("current_version"),
            effective.c.task_version_id.label("trial_version_id"),
            effective.c.task_version.label("trial_version"),
            TaskModel.run_analysis,
            TaskModel.verdict_status,
            TaskModel.verdict["verdict"].astext.label("verdict_label"),
            TaskModel.verdict["is_good"].astext.label("verdict_is_good"),
            TaskModel.verdict["confidence"].astext.label("verdict_confidence"),
            func.left(TaskModel.verdict_error, 200).label("verdict_error"),
            TaskModel.created_at,
            TaskModel.updated_at,
            func.coalesce(stats.c.total, 0).label("total"),
            func.coalesce(stats.c.completed, 0).label("completed"),
            func.coalesce(stats.c.failed, 0).label("failed"),
            func.coalesce(stats.c.skipped, 0).label("skipped"),
            func.coalesce(stats.c.pass_count, 0).label("pass_count"),
            func.coalesce(stats.c.partial_count, 0).label("partial_count"),
            func.coalesce(stats.c.fail_count, 0).label("fail_count"),
            func.coalesce(stats.c.reward_sum, 0.0).label("reward_sum"),
            func.coalesce(stats.c.reward_total, 0).label("reward_total"),
            stats.c.average_score,
        )
        .select_from(TaskModel)
        .join(
            task_experiments,
            and_(
                task_experiments.c.task_id == TaskModel.id,
                task_experiments.c.experiment_id == experiment_id,
                task_experiments.c.deleted_at.is_(None),
            ),
        )
        .outerjoin(
            current_version,
            and_(
                current_version.id == TaskModel.current_version_id,
                current_version.deleted_at.is_(None),
            ),
        )
        .outerjoin(effective, effective.c.task_id == TaskModel.id)
        .outerjoin(stats, stats.c.task_id == TaskModel.id)
        .where(TaskModel.org_id == org_id, TaskModel.deleted_at.is_(None))
    )


def _task_row(row: Mapping[str, Any]) -> ExperimentTaskRow:
    total = int(row["total"] or 0)
    terminal = sum(int(row[field] or 0) for field in ("completed", "failed", "skipped"))
    verdict_label = row["verdict_label"]
    raw_is_good = row["verdict_is_good"]
    is_good = (
        True if raw_is_good == "true" else False if raw_is_good == "false" else None
    )
    verdict = None
    if verdict_label is not None or is_good is not None or row["verdict_confidence"]:
        verdict = ExperimentPageVerdict(
            verdict=verdict_label if verdict_label in ("accept", "reject") else None,
            is_good=is_good,
            confidence=(
                str(row["verdict_confidence"])[:32]
                if row["verdict_confidence"]
                else None
            ),
        )
    values = dict(row)
    values.update(
        id=str(row["task_id"]),
        status=TaskStatus.COMPLETED if total and terminal >= total else row["status"],
        reward_success=int(row["pass_count"] or 0),
        verdict=verdict,
    )
    return ExperimentTaskRow.model_validate(values)


async def get_experiment_open_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
    limit: int = OPEN_MAX_TASKS,
    before_created_at: datetime | None = None,
    before_task_id: str | None = None,
) -> ExperimentOpenResponse:
    """Return exact experiment totals and one byte-bounded task page."""
    if (before_created_at is None) != (before_task_id is None):
        raise HTTPException(
            status_code=400, detail="Both task page fields are required"
        )
    experiment = await _member_experiment(
        session, experiment_id=experiment_id, org_id=org_id
    )
    task_rows = _experiment_task_rows(experiment_id=experiment_id, org_id=org_id)
    tasks = task_rows.subquery("experiment_open_tasks")
    inactive_verdict = or_(
        tasks.c.verdict_status.is_(None),
        tasks.c.verdict_status.not_in(_ACTIVE_VERDICT_STATUSES),
    )
    summary_result = await session.execute(
        select(
            func.count().label("task_count"),
            *(
                func.coalesce(func.sum(tasks.c[field]), 0).label(field)
                for field in (
                    "total",
                    "completed",
                    "failed",
                    "skipped",
                    "pass_count",
                    "partial_count",
                    "fail_count",
                    "reward_sum",
                    "reward_total",
                )
            ),
            func.avg(tasks.c.average_score).label("average_score"),
            func.count()
            .filter(
                inactive_verdict,
                or_(
                    tasks.c.verdict_label == "accept",
                    tasks.c.verdict_is_good == "true",
                ),
            )
            .label("qa_accepted"),
            func.count()
            .filter(
                inactive_verdict,
                or_(
                    tasks.c.verdict_label == "reject",
                    tasks.c.verdict_is_good == "false",
                ),
            )
            .label("qa_rejected"),
            func.count()
            .filter(tasks.c.verdict_status.in_(_ACTIVE_VERDICT_STATUSES))
            .label("qa_running"),
            func.count()
            .filter(
                tasks.c.verdict_status == VerdictStatus.FAILED,
                tasks.c.verdict_label.is_(None),
                tasks.c.verdict_is_good.is_(None),
            )
            .label("qa_failed"),
            select(1)
            .select_from(TrialModel)
            .join(TaskModel, TaskModel.id == TrialModel.task_id)
            .where(
                *visible_experiment_trial_predicates(experiment_id),
                TaskModel.org_id == org_id,
                TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
            )
            .exists()
            .label("has_active_trials"),
        ).select_from(tasks)
    )
    summary_row = summary_result.mappings().one()
    limit = max(1, min(limit, OPEN_MAX_TASKS))
    page_query = select(tasks)
    if before_created_at is not None:
        page_query = page_query.where(
            or_(
                tasks.c.created_at < before_created_at,
                and_(
                    tasks.c.created_at == before_created_at,
                    tasks.c.task_id < before_task_id,
                ),
            )
        )
    result = await session.execute(
        page_query.order_by(tasks.c.created_at.desc(), tasks.c.task_id.desc()).limit(
            limit + 1
        )
    )
    rows = list(result.mappings().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    trial_count = int(summary_row["total"] or 0)
    completed = int(summary_row["completed"] or 0)
    failed = int(summary_row["failed"] or 0)
    skipped = int(summary_row["skipped"] or 0)
    summary_values = dict(summary_row)
    summary_values.update(
        trial_count=trial_count,
        active=max(trial_count - completed - failed - skipped, 0),
        harness_error_count=failed,
    )
    response = ExperimentOpenResponse(
        experiment_id=str(experiment["id"]),
        name=str(experiment["name"]),
        created_at=experiment["created_at"],
        owner=experiment["owner"],
        link=experiment["link"],
        revision=experiment["revision"],
        has_active_trials=bool(summary_row["has_active_trials"]),
        summary=ExperimentPageSummary.model_validate(summary_values),
        tasks=[_task_row(row) for row in rows],
    )
    if has_more and rows:
        response.next_created_at = rows[-1]["created_at"]
        response.next_task_id = str(rows[-1]["task_id"])
    while len(response.model_dump_json().encode()) >= OPEN_MAX_BYTES:
        if len(rows) <= 1:
            raise HTTPException(
                status_code=413, detail="Experiment task shell exceeds 50 KB"
            )
        rows.pop()
        response.tasks.pop()
        has_more = True
        response.next_created_at = rows[-1]["created_at"]
        response.next_task_id = str(rows[-1]["task_id"])
    return response


async def get_experiment_trial_page_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
    limit: int = TRIAL_PAGE_MAX_TRIALS,
    before_created_at: datetime | None = None,
    before_trial_id: str | None = None,
) -> ExperimentTrialPageResponse:
    if (before_created_at is None) != (before_trial_id is None):
        raise HTTPException(
            status_code=400, detail="Both trial page fields are required"
        )
    experiment = await _member_experiment(
        session, experiment_id=experiment_id, org_id=org_id
    )
    effective = experiment_effective_versions_selectable(experiment_id=experiment_id)
    query = (
        select(
            TrialModel,
            TaskModel.task_path,
            func.left(TrialModel.analysis["classification"].astext, 100),
            func.left(TrialModel.analysis["subtype"].astext, 100),
            func.left(TrialModel.analysis["evidence"].astext, 1_000),
        )
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .join(
            effective,
            and_(
                effective.c.task_id == TrialModel.task_id,
                effective.c.task_version_id == TrialModel.task_version_id,
            ),
        )
        .where(
            *visible_experiment_trial_predicates(experiment_id),
            TaskModel.org_id == org_id,
            TaskModel.deleted_at.is_(None),
        )
        .options(load_only(*_TRIAL_PAGE_COLUMNS))
    )
    if before_created_at is not None:
        query = query.where(
            or_(
                TrialModel.created_at < before_created_at,
                and_(
                    TrialModel.created_at == before_created_at,
                    TrialModel.id < before_trial_id,
                ),
            )
        )
    limit = max(1, min(limit, TRIAL_PAGE_MAX_TRIALS))
    result = await session.execute(
        query.order_by(TrialModel.created_at.desc(), TrialModel.id.desc()).limit(
            limit + 1
        )
    )
    rows = list(result.all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    exclusions = await load_cost_exclusions(session) if rows else None
    trials = []
    for trial, task_path, classification, subtype, evidence in rows:
        analysis = {
            "classification": classification,
            "subtype": subtype,
            "evidence": evidence,
        }
        slim = build_slim_trial_response(
            trial,
            str(task_path),
            analysis=analysis,
            error_message=None,
            exclusions=exclusions,
        )
        values = slim.model_dump()
        values.update(
            analysis=ExperimentTrialAnalysis(
                status=slim.analysis_status,
                classification=classification,
                subtype=subtype,
                evidence=evidence,
                started_at=slim.analysis_started_at,
                finished_at=slim.analysis_finished_at,
            ),
        )
        trials.append(ExperimentTrialCell.model_validate(values))
    response = ExperimentTrialPageResponse(
        revision=experiment["revision"], trials=trials
    )
    if has_more and rows:
        response.next_created_at = rows[-1][0].created_at
        response.next_trial_id = rows[-1][0].id
    return response

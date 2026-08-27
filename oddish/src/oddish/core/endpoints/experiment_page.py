"""Bounded read resources for member and public experiment pages."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from oddish.config import settings
from oddish.core.baseline_gate import baseline_agent_clause
from oddish.core.cost_exclusions import CostExclusions, load_cost_exclusions
from oddish.core.experiment_membership import (
    trial_in_experiment,
    visible_grid_trial_predicates,
)
from oddish.core.helpers import experiment_effective_versions_selectable
from oddish.core.model_display_names import display_model_name, experiment_display_names
from oddish.core.sharing.helpers import get_public_experiment
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
from oddish.model_pricing import estimate_cost_usd
from oddish.schemas import (
    ExperimentOpenResponse,
    ExperimentPageSummary,
    ExperimentPageVerdict,
    ExperimentRevisionResponse,
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


@dataclass(frozen=True)
class ExperimentReadScope:
    """One resolved experiment and the audience allowed to read it."""

    experiment_id: str
    org_id: str | None
    name: str
    created_at: datetime
    owner: str | None
    link: str | None
    revision: datetime
    audience: Literal["member", "public"]
    model_display_names: dict[str, str]


def _scope_from_experiment(
    experiment: ExperimentModel, *, audience: Literal["member", "public"]
) -> ExperimentReadScope:
    return ExperimentReadScope(
        experiment_id=experiment.id,
        org_id=experiment.org_id,
        name=experiment.name,
        created_at=experiment.created_at,
        owner=experiment.owner,
        link=experiment.link,
        revision=(
            experiment.last_activity_at
            or experiment.updated_at
            or experiment.created_at
        ),
        audience=audience,
        model_display_names=(
            experiment_display_names(experiment) if audience == "public" else {}
        ),
    )


async def resolve_member_experiment_scope(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> ExperimentReadScope:
    experiment = await session.scalar(
        select(ExperimentModel).where(
            ExperimentModel.id == experiment_id,
            ExperimentModel.org_id == org_id,
            ExperimentModel.deleted_at.is_(None),
        )
    )
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return _scope_from_experiment(experiment, audience="member")


async def resolve_public_experiment_scope(
    session: AsyncSession, *, public_token: str
) -> ExperimentReadScope:
    experiment = await get_public_experiment(session, public_token)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return _scope_from_experiment(experiment, audience="public")


def _visible_grid_predicates(scope: ExperimentReadScope) -> tuple[Any, ...]:
    return visible_grid_trial_predicates(scope.experiment_id, org_id=scope.org_id)


def _active_trial_exists(scope: ExperimentReadScope):
    predicates: list[Any] = [
        trial_in_experiment(scope.experiment_id),
        TrialModel.is_probe.is_(False),
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
        TrialModel.deleted_at.is_(None),
    ]
    if scope.org_id is not None:
        predicates.append(TrialModel.org_id == scope.org_id)
    return select(1).where(*predicates).exists()


def _task_stats(scope: ExperimentReadScope, effective_versions):
    scored = and_(
        TrialModel.status == TrialStatus.SUCCESS,
        TrialModel.reward.is_not(None),
        ~baseline_agent_clause(TrialModel.agent),
    )
    return (
        select(
            TrialModel.task_id.label("task_id"),
            func.count(TrialModel.id).label("total"),
            func.count(case((TrialModel.status == TrialStatus.SUCCESS, 1))).label(
                "completed"
            ),
            func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label(
                "failed"
            ),
            func.count(case((TrialModel.status == TrialStatus.SKIPPED, 1))).label(
                "skipped"
            ),
            func.count(case((TrialModel.reward == 1, 1))).label("reward_success"),
            func.coalesce(func.sum(TrialModel.reward), 0.0).label("reward_sum"),
            func.count(case((TrialModel.reward.is_not(None), 1))).label(
                "reward_total"
            ),
            func.count(
                case(
                    (
                        and_(
                            TrialModel.status == TrialStatus.SUCCESS,
                            TrialModel.reward == 1,
                        ),
                        1,
                    )
                )
            ).label("pass_count"),
            func.count(
                case(
                    (
                        and_(
                            TrialModel.status == TrialStatus.SUCCESS,
                            TrialModel.reward.is_not(None),
                            TrialModel.reward.not_in((0, 1)),
                        ),
                        1,
                    )
                )
            ).label("partial_count"),
            func.count(
                case(
                    (
                        and_(
                            TrialModel.status == TrialStatus.SUCCESS,
                            TrialModel.reward == 0,
                        ),
                        1,
                    )
                )
            ).label("fail_count"),
            func.count(case((TrialModel.status == TrialStatus.FAILED, 1))).label(
                "harness_error_count"
            ),
            func.avg(case((scored, TrialModel.reward))).label("average_score"),
        )
        .join(
            effective_versions,
            and_(
                effective_versions.c.task_id == TrialModel.task_id,
                effective_versions.c.task_version_id.is_not_distinct_from(
                    TrialModel.task_version_id
                ),
            ),
        )
        .where(*_visible_grid_predicates(scope))
        .group_by(TrialModel.task_id)
        .subquery("experiment_task_stats")
    )


def _task_projection(scope: ExperimentReadScope, effective_versions, stats):
    current_version = aliased(TaskVersionModel)
    query = (
        select(
            TaskModel.id.label("task_id"),
            TaskModel.name.label("task_name"),
            TaskModel.status.label("task_status"),
            TaskModel.priority.label("task_priority"),
            TaskModel.user.label("task_user"),
            TaskModel.task_path.label("task_path"),
            TaskModel.current_version_id.label("current_version_id"),
            current_version.version.label("current_version"),
            effective_versions.c.task_version_id.label("trial_version_id"),
            effective_versions.c.task_version.label("trial_version"),
            TaskModel.run_analysis.label("run_analysis"),
            TaskModel.verdict_status.label("verdict_status"),
            TaskModel.verdict["verdict"].astext.label("verdict_label"),
            TaskModel.verdict["is_good"].astext.label("verdict_is_good"),
            TaskModel.verdict["confidence"].astext.label("verdict_confidence"),
            func.left(TaskModel.verdict_error, 200).label("verdict_error"),
            TaskModel.created_at.label("task_created_at"),
            TaskModel.updated_at.label("task_updated_at"),
            func.coalesce(stats.c.total, 0).label("total"),
            func.coalesce(stats.c.completed, 0).label("completed"),
            func.coalesce(stats.c.failed, 0).label("failed"),
            func.coalesce(stats.c.skipped, 0).label("skipped"),
            func.coalesce(stats.c.reward_success, 0).label("reward_success"),
            func.coalesce(stats.c.reward_sum, 0.0).label("reward_sum"),
            func.coalesce(stats.c.reward_total, 0).label("reward_total"),
            func.coalesce(stats.c.pass_count, 0).label("pass_count"),
            func.coalesce(stats.c.partial_count, 0).label("partial_count"),
            func.coalesce(stats.c.fail_count, 0).label("fail_count"),
            func.coalesce(stats.c.harness_error_count, 0).label(
                "harness_error_count"
            ),
            stats.c.average_score.label("average_score"),
        )
        .select_from(TaskModel)
        .join(
            task_experiments,
            and_(
                task_experiments.c.task_id == TaskModel.id,
                task_experiments.c.experiment_id == scope.experiment_id,
                task_experiments.c.deleted_at.is_(None),
            ),
        )
        .outerjoin(current_version, current_version.id == TaskModel.current_version_id)
        .outerjoin(effective_versions, effective_versions.c.task_id == TaskModel.id)
        .outerjoin(stats, stats.c.task_id == TaskModel.id)
        .where(TaskModel.deleted_at.is_(None))
    )
    if scope.org_id is not None:
        query = query.where(TaskModel.org_id == scope.org_id)
    return query


def _encode_cursor(**values: str) -> str:
    encoded = json.dumps(values, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_cursor(cursor: str, *, keys: set[str]) -> dict[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid experiment cursor") from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != keys
        or not all(isinstance(value, str) for value in decoded.values())
    ):
        raise HTTPException(status_code=400, detail="Invalid experiment cursor")
    return decoded


def _task_cursor(row) -> str:
    return _encode_cursor(
        created_at=row.task_created_at.isoformat(), task_id=str(row.task_id)
    )


def _trial_cursor(row) -> str:
    return _encode_cursor(
        created_at=row["created_at"].isoformat(), trial_id=str(row["id"])
    )


def _resolved_task_status(row) -> TaskStatus:
    total = int(row.total or 0)
    terminal = int(row.completed or 0) + int(row.failed or 0) + int(row.skipped or 0)
    return TaskStatus.COMPLETED if total > 0 and terminal >= total else row.task_status


def _task_row(row) -> ExperimentTaskRow:
    raw_is_good = row.verdict_is_good
    verdict_is_good = (
        True
        if raw_is_good == "true"
        else False
        if raw_is_good == "false"
        else None
    )
    verdict_label = row.verdict_label if row.verdict_label in {"accept", "reject"} else None
    verdict = (
        ExperimentPageVerdict(
            verdict=verdict_label,
            is_good=verdict_is_good,
            confidence=(
                str(row.verdict_confidence)[:32]
                if row.verdict_confidence is not None
                else None
            ),
        )
        if verdict_label is not None
        or verdict_is_good is not None
        or row.verdict_confidence is not None
        else None
    )
    return ExperimentTaskRow(
        id=str(row.task_id),
        name=str(row.task_name),
        status=_resolved_task_status(row),
        priority=row.task_priority,
        user=str(row.task_user),
        task_path=str(row.task_path),
        current_version=row.current_version,
        current_version_id=row.current_version_id,
        trial_version=row.trial_version,
        trial_version_id=row.trial_version_id,
        total=int(row.total or 0),
        completed=int(row.completed or 0),
        failed=int(row.failed or 0),
        skipped=int(row.skipped or 0),
        reward_success=int(row.reward_success or 0),
        reward_sum=float(row.reward_sum or 0.0),
        reward_total=int(row.reward_total or 0),
        run_analysis=bool(row.run_analysis),
        verdict_status=row.verdict_status,
        verdict=verdict,
        verdict_error=row.verdict_error,
        created_at=row.task_created_at,
        updated_at=row.task_updated_at,
    )


async def read_experiment_open(
    session: AsyncSession,
    *,
    scope: ExperimentReadScope,
    cursor: str | None = None,
    limit: int = OPEN_MAX_TASKS,
) -> ExperimentOpenResponse:
    """Return exact totals plus one row- and byte-bounded task page."""
    limit = max(1, min(limit, OPEN_MAX_TASKS))
    effective_versions = experiment_effective_versions_selectable(
        experiment_id=scope.experiment_id,
        org_id=scope.org_id,
    )
    stats = _task_stats(scope, effective_versions)
    all_tasks = _task_projection(scope, effective_versions, stats).subquery(
        "experiment_open_tasks"
    )
    summary_row = (
        await session.execute(
            select(
                func.count().label("task_count"),
                func.coalesce(func.sum(all_tasks.c.total), 0).label("trial_count"),
                func.coalesce(func.sum(all_tasks.c.completed), 0).label("completed"),
                func.coalesce(func.sum(all_tasks.c.failed), 0).label("failed"),
                func.coalesce(func.sum(all_tasks.c.skipped), 0).label("skipped"),
                func.coalesce(
                    func.sum(
                        all_tasks.c.total
                        - all_tasks.c.completed
                        - all_tasks.c.failed
                        - all_tasks.c.skipped
                    ),
                    0,
                ).label("active"),
                func.coalesce(func.sum(all_tasks.c.reward_success), 0).label(
                    "reward_success"
                ),
                func.coalesce(func.sum(all_tasks.c.reward_sum), 0.0).label(
                    "reward_sum"
                ),
                func.coalesce(func.sum(all_tasks.c.reward_total), 0).label(
                    "reward_total"
                ),
                func.coalesce(func.sum(all_tasks.c.pass_count), 0).label(
                    "pass_count"
                ),
                func.coalesce(func.sum(all_tasks.c.partial_count), 0).label(
                    "partial_count"
                ),
                func.coalesce(func.sum(all_tasks.c.fail_count), 0).label(
                    "fail_count"
                ),
                func.coalesce(
                    func.sum(all_tasks.c.harness_error_count), 0
                ).label("harness_error_count"),
                func.avg(all_tasks.c.average_score).label("average_score"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    or_(
                                        all_tasks.c.verdict_status.is_(None),
                                        all_tasks.c.verdict_status.not_in(
                                            _ACTIVE_VERDICT_STATUSES
                                        ),
                                    ),
                                    or_(
                                        all_tasks.c.verdict_label == "accept",
                                        and_(
                                            all_tasks.c.verdict_label.is_(None),
                                            all_tasks.c.verdict_is_good == "true",
                                        ),
                                    ),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("qa_accepted"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    or_(
                                        all_tasks.c.verdict_status.is_(None),
                                        all_tasks.c.verdict_status.not_in(
                                            _ACTIVE_VERDICT_STATUSES
                                        ),
                                    ),
                                    or_(
                                        all_tasks.c.verdict_label == "reject",
                                        and_(
                                            all_tasks.c.verdict_label.is_(None),
                                            all_tasks.c.verdict_is_good == "false",
                                        ),
                                    ),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("qa_rejected"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                all_tasks.c.verdict_status.in_(
                                    _ACTIVE_VERDICT_STATUSES
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("qa_running"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    all_tasks.c.verdict_status == VerdictStatus.FAILED,
                                    all_tasks.c.verdict_label.is_(None),
                                    all_tasks.c.verdict_is_good.is_(None),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("qa_failed"),
                _active_trial_exists(scope).label("has_active_trials"),
            ).select_from(all_tasks)
        )
    ).one()
    summary = ExperimentPageSummary(
        task_count=int(summary_row.task_count or 0),
        trial_count=int(summary_row.trial_count or 0),
        completed=int(summary_row.completed or 0),
        failed=int(summary_row.failed or 0),
        skipped=int(summary_row.skipped or 0),
        active=int(summary_row.active or 0),
        reward_success=int(summary_row.reward_success or 0),
        reward_sum=float(summary_row.reward_sum or 0.0),
        reward_total=int(summary_row.reward_total or 0),
        pass_count=int(summary_row.pass_count or 0),
        partial_count=int(summary_row.partial_count or 0),
        fail_count=int(summary_row.fail_count or 0),
        harness_error_count=int(summary_row.harness_error_count or 0),
        average_score=(
            float(summary_row.average_score)
            if summary_row.average_score is not None
            else None
        ),
        qa_accepted=int(summary_row.qa_accepted or 0),
        qa_rejected=int(summary_row.qa_rejected or 0),
        qa_running=int(summary_row.qa_running or 0),
        qa_failed=int(summary_row.qa_failed or 0),
    )

    query = _task_projection(scope, effective_versions, stats)
    if cursor is not None:
        decoded = _decode_cursor(cursor, keys={"created_at", "task_id"})
        try:
            created_at = datetime.fromisoformat(decoded["created_at"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid experiment cursor") from exc
        query = query.where(
            or_(
                TaskModel.created_at < created_at,
                and_(
                    TaskModel.created_at == created_at,
                    TaskModel.id < decoded["task_id"],
                ),
            )
        )
    rows = (
        await session.execute(
            query.order_by(TaskModel.created_at.desc(), TaskModel.id.desc()).limit(
                limit + 1
            )
        )
    ).all()

    page_rows = rows[:limit]
    tasks: list[ExperimentTaskRow] = []
    has_more = len(rows) > limit
    for row in page_rows:
        candidate = _task_row(row)
        measured = ExperimentOpenResponse(
            experiment_id=scope.experiment_id,
            name=scope.name,
            created_at=scope.created_at,
            owner=scope.owner,
            link=scope.link,
            revision=scope.revision,
            has_active_trials=bool(summary_row.has_active_trials),
            summary=summary,
            tasks=[*tasks, candidate],
            next_cursor=_task_cursor(row),
        )
        if len(measured.model_dump_json().encode()) > OPEN_MAX_BYTES:
            if not tasks:
                raise HTTPException(
                    status_code=500,
                    detail="One task row exceeds the experiment open byte limit",
                )
            has_more = True
            break
        tasks.append(candidate)

    next_cursor = _task_cursor(page_rows[len(tasks) - 1]) if tasks and has_more else None
    return ExperimentOpenResponse(
        experiment_id=scope.experiment_id,
        name=scope.name,
        created_at=scope.created_at,
        owner=scope.owner,
        link=scope.link,
        revision=scope.revision,
        has_active_trials=bool(summary_row.has_active_trials),
        summary=summary,
        tasks=tasks,
        next_cursor=next_cursor,
    )


def _resolve_cost(
    row: Mapping[str, Any], normalized_model: str | None
) -> tuple[float | None, bool | None]:
    if row["cost_usd"] is not None:
        return float(row["cost_usd"]), False
    if row["input_tokens"] is None and row["output_tokens"] is None:
        return None, None
    estimated = estimate_cost_usd(
        normalized_model or row["model"],
        row["input_tokens"],
        row["output_tokens"],
        row["cache_tokens"],
        row["cache_write_tokens"],
    )
    return (float(estimated), True) if estimated is not None else (None, None)


def _trial_cell(
    row: Mapping[str, Any],
    *,
    scope: ExperimentReadScope,
    exclusions: CostExclusions | None,
) -> ExperimentTrialCell:
    normalized_model = settings.normalize_trial_model(
        str(row["agent"]), row["model"], strict=False
    )
    cost_usd, cost_is_estimated = _resolve_cost(row, normalized_model)
    is_public = scope.audience == "public"
    model = normalized_model or row["model"]
    if is_public:
        model = display_model_name(model, scope.model_display_names)
    has_trajectory = bool(row["has_trajectory"]) or (
        str(row["agent"]).strip().lower() == "grok-build"
        and row["finished_at"] is not None
    )
    return ExperimentTrialCell(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        task_version_id=row["task_version_id"],
        name=str(row["name"]),
        agent=str(row["agent"]),
        model=model,
        provider=str(row["provider"]),
        status=row["status"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        harbor_stage=row["harbor_stage"],
        reward=float(row["reward"]) if row["reward"] is not None else None,
        input_tokens=row["input_tokens"],
        cache_tokens=row["cache_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=cost_usd,
        cost_is_estimated=cost_is_estimated,
        owned_here=None if is_public else row["home_experiment_id"] == scope.experiment_id,
        is_billed=None if is_public else row["billed_user_id"] is not None,
        cost_exclusion_reason=(
            None
            if is_public or exclusions is None
            else exclusions.reason_for(
                llm_key_hash=row["llm_key_hash"],
                model=row["model"],
                experiment_id=row["home_experiment_id"],
            )
        ),
        has_trajectory=has_trajectory,
        analysis=ExperimentTrialAnalysis(
            status=row["analysis_status"],
            classification=row["analysis_classification"],
            subtype=row["analysis_subtype"],
            started_at=row["analysis_started_at"],
            finished_at=row["analysis_finished_at"],
        ),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _trial_projection(scope: ExperimentReadScope, effective_versions):
    return (
        select(
            TrialModel.id,
            TrialModel.task_id,
            TrialModel.task_version_id,
            TrialModel.name,
            TrialModel.agent,
            TrialModel.model,
            TrialModel.provider,
            TrialModel.status,
            TrialModel.attempts,
            TrialModel.max_attempts,
            TrialModel.harbor_stage,
            TrialModel.reward,
            TrialModel.input_tokens,
            TrialModel.cache_tokens,
            TrialModel.cache_write_tokens,
            TrialModel.output_tokens,
            TrialModel.cost_usd,
            TrialModel.billed_user_id,
            TrialModel.llm_key_hash,
            TrialModel.experiment_id.label("home_experiment_id"),
            TrialModel.has_trajectory,
            TrialModel.analysis_status,
            TrialModel.analysis["classification"].astext.label(
                "analysis_classification"
            ),
            TrialModel.analysis["subtype"].astext.label("analysis_subtype"),
            TrialModel.analysis_started_at,
            TrialModel.analysis_finished_at,
            TrialModel.created_at,
            TrialModel.started_at,
            TrialModel.finished_at,
        )
        .join(
            effective_versions,
            and_(
                effective_versions.c.task_id == TrialModel.task_id,
                effective_versions.c.task_version_id.is_not_distinct_from(
                    TrialModel.task_version_id
                ),
            ),
        )
        .where(*_visible_grid_predicates(scope))
    )


async def read_experiment_trial_page(
    session: AsyncSession,
    *,
    scope: ExperimentReadScope,
    cursor: str | None = None,
    limit: int = TRIAL_PAGE_MAX_TRIALS,
) -> ExperimentTrialPageResponse:
    """Return one flat page of grid trials without wide trial columns."""
    limit = max(1, min(limit, TRIAL_PAGE_MAX_TRIALS))
    effective_versions = experiment_effective_versions_selectable(
        experiment_id=scope.experiment_id,
        org_id=scope.org_id,
    )
    query = _trial_projection(scope, effective_versions)
    if cursor is not None:
        decoded = _decode_cursor(cursor, keys={"created_at", "trial_id"})
        try:
            created_at = datetime.fromisoformat(decoded["created_at"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid experiment cursor") from exc
        query = query.where(
            or_(
                TrialModel.created_at < created_at,
                and_(
                    TrialModel.created_at == created_at,
                    TrialModel.id < decoded["trial_id"],
                ),
            )
        )
    rows = (
        await session.execute(
            query.order_by(TrialModel.created_at.desc(), TrialModel.id.desc()).limit(
                limit + 1
            )
        )
    ).mappings().all()
    page_rows = rows[:limit]
    exclusions = (
        await load_cost_exclusions(session) if scope.audience == "member" else None
    )
    return ExperimentTrialPageResponse(
        revision=scope.revision,
        trials=[
            _trial_cell(row, scope=scope, exclusions=exclusions) for row in page_rows
        ],
        next_cursor=_trial_cursor(page_rows[-1]) if len(rows) > limit else None,
    )


async def read_experiment_revision(
    session: AsyncSession, *, scope: ExperimentReadScope
) -> ExperimentRevisionResponse:
    """Return the polling resource using the same activity predicate as open."""
    return ExperimentRevisionResponse(
        revision=scope.revision,
        has_active_trials=bool(
            await session.scalar(select(_active_trial_exists(scope)))
        ),
    )

"""Bounded experiment-page reads shared by member and public routes."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from oddish.config import settings
from oddish.core.baseline_gate import baseline_agent_clause
from oddish.core.cost_exclusions import load_cost_exclusions
from oddish.core.experiment_membership import trial_in_experiment
from oddish.core.helpers import _parse_github_meta, _resolve_trial_cost
from oddish.core.model_display_names import display_model_name, experiment_display_names
from oddish.core.tags.projection import list_effective_user_tags_for_task_versions
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
    ExperimentOpenSummary,
    ExperimentRevisionResponse,
    ExperimentSlimTrial,
    ExperimentTaskShell,
    ExperimentTaskVerdict,
    ExperimentTrialPageResponse,
    ExperimentTrialTask,
    TaskBrowseExperiment,
    UserTagRef,
)

ExperimentReadAudience = Literal["member", "public"]

OPEN_MAX_TASKS = 100
OPEN_MAX_BYTES = 50_000
OPEN_DESCRIPTION_MAX_CHARS = 8_000
TRIAL_PAGE_MAX_TRIALS = 250
TRIAL_PAGE_MAX_BYTES = 300_000


@dataclass(frozen=True)
class ExperimentReadScope:
    """Resolved experiment identity plus the audience-dependent display policy."""

    experiment_id: str
    org_id: str | None
    audience: ExperimentReadAudience
    model_display_names: dict[str, str]
    name: str
    description: str | None
    is_public: bool
    owner: str | None
    link: str | None
    created_at: datetime
    revision_at: datetime

    @property
    def revision(self) -> str:
        return self.revision_at.isoformat()


def _scope_from_row(row, *, audience: ExperimentReadAudience) -> ExperimentReadScope:
    display_names = (
        experiment_display_names(
            SimpleNamespace(public_model_renames=row.public_model_renames)
        )
        if audience == "public"
        else {}
    )
    return ExperimentReadScope(
        experiment_id=str(row.id),
        org_id=str(row.org_id) if row.org_id is not None else None,
        audience=audience,
        model_display_names=display_names,
        name=str(row.name),
        description=row.description,
        is_public=bool(row.is_public),
        owner=row.owner,
        link=row.link,
        created_at=row.created_at,
        revision_at=row.last_activity_at or row.updated_at,
    )


def _experiment_scope_projection():
    return select(
        ExperimentModel.id,
        ExperimentModel.org_id,
        ExperimentModel.name,
        ExperimentModel.description,
        ExperimentModel.is_public,
        ExperimentModel.public_model_renames,
        ExperimentModel.owner,
        ExperimentModel.link,
        ExperimentModel.created_at,
        ExperimentModel.updated_at,
        ExperimentModel.last_activity_at,
    )


async def resolve_member_experiment_read_scope(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> ExperimentReadScope:
    row = (
        await session.execute(
            _experiment_scope_projection().where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.org_id == org_id,
                ExperimentModel.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return _scope_from_row(row, audience="member")


async def resolve_public_experiment_read_scope(
    session: AsyncSession, *, public_token: str
) -> ExperimentReadScope:
    row = (
        await session.execute(
            _experiment_scope_projection().where(
                ExperimentModel.public_token == public_token,
                ExperimentModel.is_public.is_(True),
                ExperimentModel.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return _scope_from_row(row, audience="public")


def _ranked_effective_versions(scope: ExperimentReadScope):
    ranked = (
        select(
            TrialModel.task_id.label("task_id"),
            TrialModel.task_version_id.label("task_version_id"),
            TaskVersionModel.version.label("trial_version"),
            func.row_number()
            .over(
                partition_by=TrialModel.task_id,
                order_by=(
                    case(
                        (TrialModel.task_version_id == TaskModel.current_version_id, 0),
                        else_=1,
                    ).asc(),
                    TaskVersionModel.version.desc(),
                    TrialModel.task_version_id.desc(),
                ),
            )
            .label("version_rank"),
        )
        .join(TaskModel, TaskModel.id == TrialModel.task_id)
        .outerjoin(TaskVersionModel, TaskVersionModel.id == TrialModel.task_version_id)
        .where(
            trial_in_experiment(scope.experiment_id),
            TrialModel.is_probe.is_(False),
            TrialModel.kind == "agent",
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.deleted_at.is_(None),
        )
        .cte("ranked_experiment_versions")
    )
    return (
        select(
            ranked.c.task_id,
            ranked.c.task_version_id,
            ranked.c.trial_version,
        )
        .where(ranked.c.version_rank == 1)
        .cte("experiment_effective_versions")
    )


def _experiment_task_stats(scope: ExperimentReadScope, effective_versions):
    scored_trial = and_(
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
            func.count(case((TrialModel.reward.is_not(None), 1))).label("reward_total"),
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
            func.avg(case((scored_trial, TrialModel.reward))).label("avg_score"),
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
        .where(
            trial_in_experiment(scope.experiment_id),
            TrialModel.is_probe.is_(False),
            TrialModel.kind == "agent",
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.deleted_at.is_(None),
        )
        .group_by(TrialModel.task_id)
        .cte("experiment_task_stats")
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
            TaskModel.tags.label("task_tags"),
            TaskModel.link.label("task_link"),
            TaskModel.task_path.label("task_path"),
            TaskModel.current_version_id.label("current_version_id"),
            current_version.version.label("current_version"),
            effective_versions.c.task_version_id.label("trial_version_id"),
            effective_versions.c.trial_version.label("trial_version"),
            TaskModel.run_analysis.label("run_analysis"),
            TaskModel.run_probe.label("run_probe"),
            TaskModel.verdict_status.label("verdict_status"),
            TaskModel.verdict["verdict"].astext.label("verdict_label"),
            case(
                (TaskModel.verdict["is_good"].astext == "true", True),
                (TaskModel.verdict["is_good"].astext == "false", False),
                else_=None,
            ).label("verdict_is_good"),
            TaskModel.verdict["confidence"].astext.label("verdict_confidence"),
            case(
                (
                    and_(
                        TaskModel.verdict_status == VerdictStatus.SUCCESS,
                        TaskModel.verdict["is_good"].astext == "true",
                    ),
                    1,
                ),
                else_=0,
            ).label("qa_accepted"),
            case(
                (
                    and_(
                        TaskModel.verdict_status == VerdictStatus.SUCCESS,
                        TaskModel.verdict["is_good"].astext == "false",
                    ),
                    1,
                ),
                else_=0,
            ).label("qa_rejected"),
            case(
                (
                    or_(
                        TaskModel.status == TaskStatus.VERDICT_PENDING,
                        TaskModel.verdict_status.in_(
                            (
                                VerdictStatus.PENDING,
                                VerdictStatus.QUEUED,
                                VerdictStatus.RUNNING,
                            )
                        ),
                    ),
                    1,
                ),
                else_=0,
            ).label("qa_running"),
            case((TaskModel.verdict_status == VerdictStatus.FAILED, 1), else_=0).label(
                "qa_failed"
            ),
            TaskModel.created_at.label("task_created_at"),
            TaskModel.updated_at.label("task_updated_at"),
            TaskModel.started_at.label("task_started_at"),
            TaskModel.finished_at.label("task_finished_at"),
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
            func.coalesce(stats.c.harness_error_count, 0).label("harness_error_count"),
            stats.c.avg_score.label("avg_score"),
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


def _encode_cursor(payload: dict[str, str]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> dict[str, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400, detail="Invalid experiment cursor"
        ) from exc
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in decoded.items()
    ):
        raise HTTPException(status_code=400, detail="Invalid experiment cursor")
    return decoded


def _task_cursor(row) -> str:
    return _encode_cursor(
        {"task_created_at": row.task_created_at.isoformat(), "task_id": row.task_id}
    )


def _trial_cursor(row) -> str:
    return _encode_cursor(
        {
            "task_created_at": row.task_created_at.isoformat(),
            "task_id": row.task_id,
            "trial_created_at": row.trial_created_at.isoformat(),
            "trial_id": row.trial_id,
        }
    )


def _user_tag_refs(views) -> list[UserTagRef]:
    return [
        UserTagRef(
            tag_id=view.tag_id,
            key=view.key,
            value=view.value,
            color=view.color,
            visibility=view.visibility,
            current=view.current,
            older=view.older,
        )
        for view in views
    ]


def _resolved_task_status(row) -> TaskStatus:
    total = int(row.total or 0)
    terminal = int(row.completed or 0) + int(row.failed or 0) + int(row.skipped or 0)
    if total > 0 and terminal >= total:
        return TaskStatus.COMPLETED
    return row.task_status


def _task_shell(
    row, scope: ExperimentReadScope, user_tags_by_task: dict[str, list]
) -> ExperimentTaskShell:
    completed = int(row.completed or 0)
    total = int(row.total or 0)
    tags = row.task_tags or {}
    verdict_label = (
        row.verdict_label if row.verdict_label in ("accept", "reject") else None
    )
    verdict = (
        ExperimentTaskVerdict(
            verdict=verdict_label,
            is_good=row.verdict_is_good,
            confidence=str(row.verdict_confidence)[:32]
            if row.verdict_confidence is not None
            else None,
        )
        if verdict_label is not None
        or row.verdict_is_good is not None
        or row.verdict_confidence is not None
        else None
    )
    return ExperimentTaskShell(
        id=str(row.task_id),
        name=str(row.task_name),
        status=_resolved_task_status(row),
        priority=row.task_priority,
        user=str(row.task_user),
        github_username=tags.get("github_username"),
        github_meta=_parse_github_meta(tags),
        link=row.task_link,
        task_path=str(row.task_path),
        experiment_id=scope.experiment_id,
        experiment_name=scope.name,
        experiment_is_public=scope.is_public,
        experiment_created_at=scope.created_at,
        experiment_owner=scope.owner,
        experiment_link=scope.link,
        experiments=[TaskBrowseExperiment(id=scope.experiment_id, name=scope.name)],
        current_version=row.current_version,
        current_version_id=row.current_version_id,
        trial_version=row.trial_version,
        trial_version_id=row.trial_version_id,
        total=total,
        completed=completed,
        failed=int(row.failed or 0),
        skipped=int(row.skipped or 0),
        progress=f"{completed}/{total} completed",
        reward_success=int(row.reward_success or 0),
        reward_sum=float(row.reward_sum or 0.0),
        reward_total=int(row.reward_total or 0),
        run_analysis=bool(row.run_analysis),
        run_probe=bool(row.run_probe),
        verdict_status=row.verdict_status,
        verdict=verdict,
        user_tags=_user_tag_refs(user_tags_by_task.get(str(row.task_id), [])),
        created_at=row.task_created_at,
        updated_at=row.task_updated_at,
        started_at=row.task_started_at,
        finished_at=row.task_finished_at,
    )


async def get_experiment_open(
    session: AsyncSession,
    *,
    scope: ExperimentReadScope,
    cursor: str | None = None,
    max_tasks: int = OPEN_MAX_TASKS,
) -> ExperimentOpenResponse:
    """Return exact summary plus a byte-bounded page of lightweight tasks."""

    max_tasks = max(1, min(max_tasks, OPEN_MAX_TASKS))
    effective_versions = _ranked_effective_versions(scope)
    stats = _experiment_task_stats(scope, effective_versions)
    all_tasks = _task_projection(scope, effective_versions, stats).subquery(
        "experiment_open_all_tasks"
    )
    summary_row = (
        await session.execute(
            select(
                func.count().label("task_count"),
                func.coalesce(func.sum(all_tasks.c.total), 0).label("trial_count"),
                func.coalesce(func.sum(all_tasks.c.completed), 0).label(
                    "success_count"
                ),
                func.coalesce(func.sum(all_tasks.c.failed), 0).label("failed_count"),
                func.coalesce(func.sum(all_tasks.c.skipped), 0).label("skipped_count"),
                func.coalesce(
                    func.sum(
                        all_tasks.c.total
                        - all_tasks.c.completed
                        - all_tasks.c.failed
                        - all_tasks.c.skipped
                    ),
                    0,
                ).label("active_count"),
                func.coalesce(func.sum(all_tasks.c.reward_success), 0).label(
                    "reward_success"
                ),
                func.coalesce(func.sum(all_tasks.c.reward_sum), 0.0).label(
                    "reward_sum"
                ),
                func.coalesce(func.sum(all_tasks.c.reward_total), 0).label(
                    "reward_total"
                ),
                func.coalesce(func.sum(all_tasks.c.pass_count), 0).label("pass_count"),
                func.coalesce(func.sum(all_tasks.c.partial_count), 0).label(
                    "partial_count"
                ),
                func.coalesce(func.sum(all_tasks.c.fail_count), 0).label("fail_count"),
                func.coalesce(func.sum(all_tasks.c.harness_error_count), 0).label(
                    "harness_error_count"
                ),
                func.avg(all_tasks.c.avg_score).label("avg_score"),
                func.coalesce(func.sum(all_tasks.c.qa_accepted), 0).label(
                    "qa_accepted"
                ),
                func.coalesce(func.sum(all_tasks.c.qa_rejected), 0).label(
                    "qa_rejected"
                ),
                func.coalesce(func.sum(all_tasks.c.qa_running), 0).label("qa_running"),
                func.coalesce(func.sum(all_tasks.c.qa_failed), 0).label("qa_failed"),
            ).select_from(all_tasks)
        )
    ).one()
    summary = ExperimentOpenSummary(
        task_count=int(summary_row.task_count or 0),
        trial_count=int(summary_row.trial_count or 0),
        success_count=int(summary_row.success_count or 0),
        failed_count=int(summary_row.failed_count or 0),
        skipped_count=int(summary_row.skipped_count or 0),
        active_count=int(summary_row.active_count or 0),
        reward_success=int(summary_row.reward_success or 0),
        reward_sum=float(summary_row.reward_sum or 0.0),
        reward_total=int(summary_row.reward_total or 0),
        pass_count=int(summary_row.pass_count or 0),
        partial_count=int(summary_row.partial_count or 0),
        fail_count=int(summary_row.fail_count or 0),
        harness_error_count=int(summary_row.harness_error_count or 0),
        avg_score=(
            float(summary_row.avg_score) if summary_row.avg_score is not None else None
        ),
        qa_accepted=int(summary_row.qa_accepted or 0),
        qa_rejected=int(summary_row.qa_rejected or 0),
        qa_running=int(summary_row.qa_running or 0),
        qa_failed=int(summary_row.qa_failed or 0),
    )

    query = _task_projection(scope, effective_versions, stats)
    if cursor:
        decoded = _decode_cursor(cursor)
        try:
            task_created_at = datetime.fromisoformat(decoded["task_created_at"])
            task_id = decoded["task_id"]
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="Invalid experiment cursor"
            ) from exc
        query = query.where(
            or_(
                TaskModel.created_at < task_created_at,
                and_(TaskModel.created_at == task_created_at, TaskModel.id < task_id),
            )
        )
    rows = (
        await session.execute(
            query.order_by(TaskModel.created_at.desc(), TaskModel.id.desc()).limit(
                max_tasks + 1
            )
        )
    ).all()

    page_rows = rows[:max_tasks]
    task_ids = [str(row.task_id) for row in page_rows]
    user_tags_by_task = (
        await list_effective_user_tags_for_task_versions(
            session,
            task_ids=task_ids,
            public_only=scope.audience == "public",
        )
        if task_ids
        else {}
    )
    description = scope.description
    description_truncated = bool(
        description and len(description) > OPEN_DESCRIPTION_MAX_CHARS
    )
    if description_truncated:
        description = description[:OPEN_DESCRIPTION_MAX_CHARS]

    tasks: list[ExperimentTaskShell] = []
    more_rows = len(rows) > max_tasks
    for row in page_rows:
        candidate = _task_shell(row, scope, user_tags_by_task)
        candidate_response = ExperimentOpenResponse(
            experiment_id=scope.experiment_id,
            name=scope.name,
            description=description,
            description_truncated=description_truncated,
            revision=scope.revision,
            has_active_trials=summary.active_count > 0,
            summary=summary,
            tasks=[*tasks, candidate],
            next_cursor=_task_cursor(row),
        )
        candidate_size = len(
            candidate_response.model_dump_json(exclude_none=True).encode("utf-8")
        )
        if candidate_size > OPEN_MAX_BYTES:
            if not tasks:
                raise HTTPException(
                    status_code=500,
                    detail="One task shell exceeds the experiment open byte limit",
                )
            more_rows = True
            break
        tasks.append(candidate)

    next_cursor = (
        _task_cursor(page_rows[len(tasks) - 1]) if tasks and more_rows else None
    )
    return ExperimentOpenResponse(
        experiment_id=scope.experiment_id,
        name=scope.name,
        description=description,
        description_truncated=description_truncated,
        revision=scope.revision,
        has_active_trials=summary.active_count > 0,
        summary=summary,
        tasks=tasks,
        next_cursor=next_cursor,
    )


def _mask_queue_key(queue_key: str, names: dict[str, str]) -> str:
    normalized = settings.normalize_queue_key(queue_key)
    lowered = normalized.strip().lower()
    for model_key in sorted(names, key=len, reverse=True):
        display = names[model_key]
        if lowered == model_key:
            return display
        suffix = f"/{model_key}"
        if lowered.endswith(suffix):
            return f"{normalized[: -len(model_key)]}{display}"
    return normalized


async def get_experiment_trial_page(
    session: AsyncSession,
    *,
    scope: ExperimentReadScope,
    cursor: str | None = None,
) -> ExperimentTrialPageResponse:
    """Return at most 250 projected trials in stable task/trial order."""

    effective_versions = _ranked_effective_versions(scope)
    stats = _experiment_task_stats(scope, effective_versions)
    task_rows = _task_projection(scope, effective_versions, stats).subquery(
        "experiment_trial_tasks"
    )
    query = (
        select(
            *task_rows.c,
            TrialModel.id.label("trial_id"),
            TrialModel.name.label("trial_name"),
            TrialModel.experiment_id.label("trial_experiment_id"),
            TrialModel.agent.label("trial_agent"),
            TrialModel.provider.label("trial_provider"),
            TrialModel.queue_key.label("trial_queue_key"),
            TrialModel.model.label("trial_model"),
            TrialModel.environment.label("trial_environment"),
            TrialModel.status.label("trial_status"),
            TrialModel.attempts.label("trial_attempts"),
            TrialModel.max_attempts.label("trial_max_attempts"),
            TrialModel.harbor_stage.label("trial_harbor_stage"),
            TrialModel.reward.label("trial_reward"),
            TrialModel.kind.label("trial_kind"),
            TrialModel.input_tokens.label("trial_input_tokens"),
            TrialModel.cache_tokens.label("trial_cache_tokens"),
            TrialModel.cache_write_tokens.label("trial_cache_write_tokens"),
            TrialModel.output_tokens.label("trial_output_tokens"),
            TrialModel.cost_usd.label("trial_cost_usd"),
            TrialModel.billed_user_id.label("trial_billed_user_id"),
            TrialModel.llm_key_hash.label("trial_llm_key_hash"),
            TrialModel.has_trajectory.label("trial_has_trajectory"),
            TrialModel.analysis_status.label("trial_analysis_status"),
            TrialModel.analysis["classification"].astext.label(
                "trial_analysis_classification"
            ),
            TrialModel.analysis["subtype"].astext.label("trial_analysis_subtype"),
            TrialModel.analysis_started_at.label("trial_analysis_started_at"),
            TrialModel.analysis_finished_at.label("trial_analysis_finished_at"),
            TrialModel.created_at.label("trial_created_at"),
            TrialModel.started_at.label("trial_started_at"),
            TrialModel.finished_at.label("trial_finished_at"),
        )
        .select_from(task_rows)
        .join(
            TrialModel,
            and_(
                TrialModel.task_id == task_rows.c.task_id,
                TrialModel.task_version_id.is_not_distinct_from(
                    task_rows.c.trial_version_id
                ),
            ),
        )
        .where(
            trial_in_experiment(scope.experiment_id),
            TrialModel.is_probe.is_(False),
            TrialModel.kind == "agent",
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.deleted_at.is_(None),
        )
    )
    if cursor:
        decoded = _decode_cursor(cursor)
        try:
            task_created_at = datetime.fromisoformat(decoded["task_created_at"])
            task_id = decoded["task_id"]
            trial_created_at = datetime.fromisoformat(decoded["trial_created_at"])
            trial_id = decoded["trial_id"]
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="Invalid experiment cursor"
            ) from exc
        query = query.where(
            or_(
                task_rows.c.task_created_at < task_created_at,
                and_(
                    task_rows.c.task_created_at == task_created_at,
                    task_rows.c.task_id < task_id,
                ),
                and_(
                    task_rows.c.task_created_at == task_created_at,
                    task_rows.c.task_id == task_id,
                    TrialModel.created_at < trial_created_at,
                ),
                and_(
                    task_rows.c.task_created_at == task_created_at,
                    task_rows.c.task_id == task_id,
                    TrialModel.created_at == trial_created_at,
                    TrialModel.id < trial_id,
                ),
            )
        )
    rows = (
        await session.execute(
            query.order_by(
                task_rows.c.task_created_at.desc(),
                task_rows.c.task_id.desc(),
                TrialModel.created_at.desc(),
                TrialModel.id.desc(),
            ).limit(TRIAL_PAGE_MAX_TRIALS + 1)
        )
    ).all()
    selected_rows = list(rows[:TRIAL_PAGE_MAX_TRIALS])
    has_more = len(rows) > TRIAL_PAGE_MAX_TRIALS

    exclusions = (
        await load_cost_exclusions(session) if scope.audience == "member" else None
    )

    def build_response(page_rows) -> ExperimentTrialPageResponse:
        tasks_by_id: dict[str, ExperimentTrialTask] = {}
        for row in page_rows:
            task_id = str(row.task_id)
            task = tasks_by_id.get(task_id)
            if task is None:
                task = ExperimentTrialTask(
                    **_task_shell(row, scope, {}).model_dump(), trials=[]
                )
                tasks_by_id[task_id] = task

            normalized_model = settings.normalize_trial_model(
                row.trial_agent, row.trial_model, strict=False
            )
            cost_trial = SimpleNamespace(
                cost_usd=row.trial_cost_usd,
                input_tokens=row.trial_input_tokens,
                output_tokens=row.trial_output_tokens,
                cache_tokens=row.trial_cache_tokens,
                cache_write_tokens=row.trial_cache_write_tokens,
                model=row.trial_model,
            )
            cost_usd, cost_is_estimated = _resolve_trial_cost(
                cost_trial, normalized_model
            )
            display_model = (
                display_model_name(normalized_model, scope.model_display_names)
                if scope.audience == "public"
                else normalized_model
            )
            task.trials.append(
                ExperimentSlimTrial(
                    id=str(row.trial_id),
                    name=str(row.trial_name),
                    task_id=task_id,
                    task_path=str(row.task_path),
                    task_version=row.trial_version,
                    task_version_id=row.trial_version_id,
                    experiment_id=(
                        scope.experiment_id
                        if scope.audience == "public"
                        else row.trial_experiment_id
                    ),
                    agent=str(row.trial_agent),
                    provider=str(row.trial_provider),
                    queue_key=_mask_queue_key(
                        str(row.trial_queue_key), scope.model_display_names
                    ),
                    model=display_model,
                    environment=row.trial_environment,
                    status=row.trial_status,
                    attempts=int(row.trial_attempts),
                    max_attempts=int(row.trial_max_attempts),
                    harbor_stage=row.trial_harbor_stage,
                    reward=row.trial_reward,
                    kind=row.trial_kind or "agent",
                    input_tokens=row.trial_input_tokens,
                    cache_tokens=row.trial_cache_tokens,
                    output_tokens=row.trial_output_tokens,
                    cost_usd=cost_usd,
                    cost_is_estimated=cost_is_estimated,
                    is_billed=(
                        row.trial_billed_user_id is not None
                        if scope.audience == "member"
                        else False
                    ),
                    cost_exclusion_reason=(
                        exclusions.reason_for(
                            llm_key_hash=row.trial_llm_key_hash,
                            model=row.trial_model,
                            experiment_id=row.trial_experiment_id,
                        )
                        if exclusions is not None
                        else None
                    ),
                    has_trajectory=bool(row.trial_has_trajectory),
                    analysis_status=row.trial_analysis_status,
                    analysis_classification=row.trial_analysis_classification,
                    analysis_subtype=row.trial_analysis_subtype,
                    analysis_started_at=row.trial_analysis_started_at,
                    analysis_finished_at=row.trial_analysis_finished_at,
                    created_at=row.trial_created_at,
                    started_at=row.trial_started_at,
                    finished_at=row.trial_finished_at,
                )
            )
        return ExperimentTrialPageResponse(
            revision=scope.revision,
            tasks=list(tasks_by_id.values()),
            trial_count=len(page_rows),
            next_cursor=_trial_cursor(page_rows[-1])
            if page_rows and has_more
            else None,
        )

    response = build_response(selected_rows)
    while (
        len(response.model_dump_json(exclude_none=True).encode("utf-8"))
        > TRIAL_PAGE_MAX_BYTES
    ):
        if len(selected_rows) <= 1:
            raise HTTPException(
                status_code=500,
                detail="One slim trial exceeds the experiment page byte limit",
            )
        selected_rows.pop()
        has_more = True
        response = build_response(selected_rows)
    return response


async def get_experiment_revision(
    session: AsyncSession, *, scope: ExperimentReadScope
) -> ExperimentRevisionResponse:
    """Return only the activity revision and whether visible trials are active."""

    active = await session.scalar(
        select(
            select(1)
            .where(
                trial_in_experiment(scope.experiment_id),
                TrialModel.is_probe.is_(False),
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
                TrialModel.deleted_at.is_(None),
            )
            .exists()
        )
    )
    return ExperimentRevisionResponse(
        revision=scope.revision,
        has_active_trials=bool(active),
    )

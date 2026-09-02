from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from oddish.config import settings
from oddish.core.baseline_gate import baseline_agent_clause
from oddish.core.cost_exclusions import CostExclusions, load_cost_exclusions
from oddish.core.experiment_membership import experiment_trial_scope
from oddish.core.helpers import (
    SLIM_TRIAL_RESPONSE_COLUMNS,
    _parse_github_meta,
    experiment_visible_trials_selectable,
)
from oddish.core.model_display_names import (
    apply_model_display_names,
    experiment_display_names,
)
from oddish.core.sharing.helpers import get_public_experiment
from oddish.core.sharing.public_projection import public_task_github_meta
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
    ExperimentFocusResponse,
    ExperimentOpenResponse,
    ExperimentPageSummary,
    ExperimentPageVerdict,
    ExperimentTaskRow,
    ExperimentTrialAnalysis,
    ExperimentTrialCell,
    ExperimentTrialPageResponse,
    PublicExperimentFocusResponse,
    PublicExperimentOpenResponse,
    PublicExperimentTaskRow,
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


def build_experiment_trial_cell(
    row: Mapping[str, Any],
    *,
    exclusions: CostExclusions | None,
) -> ExperimentTrialCell:
    """Project one bounded SQL row without constructing an ORM entity."""
    normalized_model = settings.normalize_trial_model(
        str(row["agent"]), row["model"], strict=False
    )
    stored_cost = row["cost_usd"]
    if stored_cost is not None:
        cost_usd = float(stored_cost)
        cost_is_estimated: bool | None = False
    elif row["input_tokens"] is None and row["output_tokens"] is None:
        cost_usd = None
        cost_is_estimated = None
    else:
        cost_usd = estimate_cost_usd(
            normalized_model or row["model"],
            row["input_tokens"],
            row["output_tokens"],
            row["cache_tokens"],
            row["cache_write_tokens"],
        )
        cost_is_estimated = True if cost_usd is not None else None

    experiment_id = row["experiment_id"]
    return ExperimentTrialCell(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        task_path=str(row["task_path"]),
        experiment_id=str(experiment_id) if experiment_id is not None else None,
        task_version_id=row["task_version_id"],
        name=str(row["name"]),
        agent=str(row["agent"]),
        model=normalized_model,
        provider=str(row["provider"]),
        queue_key=settings.normalize_queue_key(str(row["queue_key"])),
        status=row["status"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        harbor_stage=row["harbor_stage"],
        reward=row["reward"],
        input_tokens=row["input_tokens"],
        cache_tokens=row["cache_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=cost_usd,
        cost_is_estimated=cost_is_estimated,
        is_billed=row["billed_user_id"] is not None,
        cost_exclusion_reason=(
            exclusions.reason_for(
                llm_key_hash=row["llm_key_hash"],
                model=row["model"],
                experiment_id=experiment_id,
            )
            if exclusions
            else None
        ),
        has_trajectory=bool(row["has_trajectory"]),
        analysis=ExperimentTrialAnalysis(
            status=row["analysis_status"],
            classification=row["analysis_classification"],
            subtype=row["analysis_subtype"],
            evidence=row["analysis_evidence"],
            started_at=row["analysis_started_at"],
            finished_at=row["analysis_finished_at"],
        ),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
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


def _experiment_task_rows(
    *,
    experiment_id: str,
    org_id: str,
    task_ids: Sequence[str] | None = None,
    include_user: bool = False,
):
    selected_task_ids = list(task_ids) if task_ids is not None else None
    # The experiment's visible trials come from one indexed membership
    # subquery, already annotated with each task's effective version, so the
    # stats are one grouped pass with no join back to the trials table.
    scope = experiment_trial_scope(experiment_id, org_id=org_id)
    visible = experiment_visible_trials_selectable(scope, task_ids=selected_task_ids).c
    scored = and_(
        visible.status == TrialStatus.SUCCESS,
        visible.reward.is_not(None),
        ~baseline_agent_clause(visible.agent),
    )
    stats = (
        select(
            visible.task_id.label("task_id"),
            # Every row of a task carries the same effective version, so the
            # aggregate simply repeats it for the task shell.
            func.max(visible.effective_task_version_id).label("trial_version_id"),
            func.max(visible.effective_task_version).label("trial_version"),
            func.count().label("total"),
            func.count()
            .filter(visible.status == TrialStatus.SUCCESS)
            .label("completed"),
            func.count().filter(visible.status == TrialStatus.FAILED).label("failed"),
            func.count().filter(visible.status == TrialStatus.SKIPPED).label("skipped"),
            func.count()
            .filter(visible.status == TrialStatus.SUCCESS, visible.reward == 1)
            .label("pass_count"),
            func.count()
            .filter(
                visible.status == TrialStatus.SUCCESS,
                visible.reward.is_not(None),
                visible.reward.not_in((0, 1)),
            )
            .label("partial_count"),
            func.count()
            .filter(visible.status == TrialStatus.SUCCESS, visible.reward == 0)
            .label("fail_count"),
            func.coalesce(
                func.sum(visible.reward).filter(visible.status == TrialStatus.SUCCESS),
                0.0,
            ).label("reward_sum"),
            func.count(visible.reward)
            .filter(visible.status == TrialStatus.SUCCESS)
            .label("reward_total"),
            func.avg(case((scored, visible.reward))).label("average_score"),
        )
        .where(
            # Match the existing experiment-shell contract: use the selected
            # version when one exists, but retain legacy/versionless trials
            # when the selector has no live version for this task.
            or_(
                visible.effective_task_version_id.is_(None),
                visible.effective_task_version_id == visible.task_version_id,
            ),
        )
        .group_by(visible.task_id)
        .subquery("experiment_task_stats")
    )
    current_version = aliased(TaskVersionModel)
    columns = [
        TaskModel.id.label("task_id"),
        TaskModel.name,
        TaskModel.status,
        TaskModel.priority,
        TaskModel.task_path,
        TaskModel.tags,
        TaskModel.current_version_id,
        current_version.version.label("current_version"),
        stats.c.trial_version_id,
        stats.c.trial_version,
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
    ]
    if include_user:
        columns.append(TaskModel.user)
    return (
        select(*columns)
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
        .outerjoin(stats, stats.c.task_id == TaskModel.id)
        .where(
            TaskModel.org_id == org_id,
            TaskModel.deleted_at.is_(None),
            *(
                (TaskModel.id.in_(selected_task_ids),)
                if selected_task_ids is not None
                else ()
            ),
        )
    )


def _task_row_values(row: Mapping[str, Any]) -> dict[str, Any]:
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
    return values


def _task_row(row: Mapping[str, Any]) -> ExperimentTaskRow:
    values = _task_row_values(row)
    values["github_meta"] = _parse_github_meta(row["tags"])
    return ExperimentTaskRow.model_validate(values)


def _public_task_row(row: Mapping[str, Any]) -> PublicExperimentTaskRow:
    values = _task_row_values(row)
    values["github_meta"] = public_task_github_meta(_parse_github_meta(row["tags"]))
    return PublicExperimentTaskRow.model_validate(values)


async def get_experiment_open_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None,
    limit: int = OPEN_MAX_TASKS,
    before_created_at: datetime | None = None,
    before_task_id: str | None = None,
    include_summary: bool = True,
    _experiment: Mapping[str, Any] | None = None,
    _public: bool = False,
) -> ExperimentOpenResponse | PublicExperimentOpenResponse:
    """Return exact experiment totals and one byte-bounded task page."""
    if (before_created_at is None) != (before_task_id is None):
        raise HTTPException(
            status_code=400, detail="Both task page fields are required"
        )
    if _experiment is None:
        if org_id is None:
            raise ValueError("Member experiment reads require an organization")
        experiment = await _member_experiment(
            session, experiment_id=experiment_id, org_id=org_id
        )
    else:
        experiment = _experiment
    limit = max(1, min(limit, OPEN_MAX_TASKS))
    page_query = (
        select(TaskModel.id.label("task_id"), TaskModel.created_at)
        .join(
            task_experiments,
            and_(
                task_experiments.c.task_id == TaskModel.id,
                task_experiments.c.experiment_id == experiment_id,
                task_experiments.c.deleted_at.is_(None),
            ),
        )
        .where(TaskModel.org_id == org_id, TaskModel.deleted_at.is_(None))
    )
    if before_created_at is not None:
        page_query = page_query.where(
            or_(
                TaskModel.created_at < before_created_at,
                and_(
                    TaskModel.created_at == before_created_at,
                    TaskModel.id < before_task_id,
                ),
            )
        )
    page_result = await session.execute(
        page_query.order_by(TaskModel.created_at.desc(), TaskModel.id.desc()).limit(
            limit + 1
        )
    )
    page_id_rows = list(page_result.mappings().all())
    has_more = len(page_id_rows) > limit
    page_id_rows = page_id_rows[:limit]
    selected_task_ids = [str(row["task_id"]) for row in page_id_rows]

    rows: list[Mapping[str, Any]] = []
    if selected_task_ids:
        task_result = await session.execute(
            _experiment_task_rows(
                experiment_id=experiment_id,
                org_id=org_id,
                task_ids=selected_task_ids,
                include_user=not _public,
            )
        )
        rows_by_id = {str(row["task_id"]): row for row in task_result.mappings().all()}
        page_id_rows = [
            row for row in page_id_rows if str(row["task_id"]) in rows_by_id
        ]
        selected_task_ids = [str(row["task_id"]) for row in page_id_rows]
        rows = [rows_by_id[task_id] for task_id in selected_task_ids]

    summary_row: Mapping[str, Any] | None = None
    summary = None
    if include_summary:
        tasks = _experiment_task_rows(
            experiment_id=experiment_id, org_id=org_id
        ).subquery("experiment_open_tasks")
        active_scope = experiment_trial_scope(experiment_id, org_id=org_id)
        active_trials = active_scope.trials
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
                .select_from(active_trials)
                .join(TaskModel, TaskModel.id == active_trials.task_id)
                .where(
                    *active_scope.visible_predicates(),
                    TaskModel.org_id == org_id,
                    active_trials.status.in_(ACTIVE_TRIAL_STATUSES),
                )
                .exists()
                .label("has_active_trials"),
            ).select_from(tasks)
        )
        summary_row = summary_result.mappings().one()
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
        summary = ExperimentPageSummary.model_validate(summary_values)

    response_type = PublicExperimentOpenResponse if _public else ExperimentOpenResponse
    response = response_type(
        experiment_id=str(experiment["id"]),
        name=str(experiment["name"]),
        created_at=experiment["created_at"],
        revision=experiment["revision"],
        # QA starts only after the visible agent trials settle. Keep clients
        # polling while that replacement verdict is active as well, otherwise
        # the first ``qa_running`` response would stop its own refresh loop.
        has_active_trials=(
            bool(summary_row and summary_row["has_active_trials"])
            or bool(summary_row and int(summary_row["qa_running"] or 0) > 0)
        ),
        summary=summary,
        tasks=[_public_task_row(row) if _public else _task_row(row) for row in rows],
        **(
            {}
            if _public
            else {"owner": experiment["owner"], "link": experiment["link"]}
        ),
    )
    if has_more and rows:
        response.next_created_at = page_id_rows[len(rows) - 1]["created_at"]
        response.next_task_id = str(page_id_rows[len(rows) - 1]["task_id"])
    while len(response.model_dump_json().encode()) >= OPEN_MAX_BYTES:
        if len(rows) <= 1:
            raise HTTPException(
                status_code=413, detail="Experiment task shell exceeds 50 KB"
            )
        rows.pop()
        response.tasks.pop()
        has_more = True
        response.next_created_at = page_id_rows[len(rows) - 1]["created_at"]
        response.next_task_id = str(page_id_rows[len(rows) - 1]["task_id"])
    return response


def _trial_column(source: Any, key: str) -> Any:
    """Column ``key`` of a ``TrialModel`` alias or of a subquery of its columns."""
    return source.c[key] if hasattr(source, "c") else getattr(source, key)


def _experiment_trial_projection(trials: Any):
    """Bounded trial columns read from ``trials`` (see :func:`_trial_column`)."""
    analysis = _trial_column(trials, "analysis")
    return (
        select(
            *(
                _trial_column(trials, column.key).label(column.key)
                for column in _TRIAL_PAGE_COLUMNS
            ),
            TaskModel.task_path,
            func.left(analysis["classification"].astext, 100).label(
                "analysis_classification"
            ),
            func.left(analysis["subtype"].astext, 100).label("analysis_subtype"),
            func.left(analysis["evidence"].astext, 1_000).label("analysis_evidence"),
        )
        .select_from(trials)
        .join(TaskModel, TaskModel.id == _trial_column(trials, "task_id"))
    )


def _experiment_trial_rows_query(*, experiment_id: str, org_id: str):
    """Grid-visible trial rows, plus the columns callers filter and order on."""
    scope = experiment_trial_scope(experiment_id, org_id=org_id)
    visible = experiment_visible_trials_selectable(
        scope, columns=(*_TRIAL_PAGE_COLUMNS, TrialModel.analysis)
    )
    query = _experiment_trial_projection(visible).where(
        or_(
            visible.c.effective_task_version_id.is_(None),
            visible.c.effective_task_version_id == visible.c.task_version_id,
        ),
        TaskModel.org_id == org_id,
        TaskModel.deleted_at.is_(None),
    )
    return query, visible.c


def _member_experiment_focus_trial_query(*, experiment_id: str, org_id: str):
    """Address historical member trials without widening public grid visibility."""
    scope = experiment_trial_scope(experiment_id, org_id=org_id)
    trials = scope.trials
    query = _experiment_trial_projection(trials).where(
        *scope.member_predicates(),
        trials.deleted_at.is_(None),
        TaskModel.org_id == org_id,
        TaskModel.deleted_at.is_(None),
    )
    return query, trials


async def get_experiment_focus_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None,
    task_selector: str | None = None,
    trial_id: str | None = None,
    _experiment: Mapping[str, Any] | None = None,
    _include_cost_exclusion_labels: bool = True,
    _require_grid_trial_visibility: bool = False,
    _public: bool = False,
) -> ExperimentFocusResponse | PublicExperimentFocusResponse:
    """Resolve one URL-addressed task and optional trial within an experiment."""
    if not task_selector and not trial_id:
        raise HTTPException(status_code=400, detail="A task or trial is required")
    is_member_read = _experiment is None
    if is_member_read:
        if org_id is None:
            raise ValueError("Member experiment reads require an organization")
        experiment = await _member_experiment(
            session, experiment_id=experiment_id, org_id=org_id
        )
    else:
        experiment = _experiment

    trial_row = None
    if trial_id:
        trial_query, trials = (
            _experiment_trial_rows_query(experiment_id=experiment_id, org_id=org_id)
            if _require_grid_trial_visibility
            else _member_experiment_focus_trial_query(
                experiment_id=experiment_id, org_id=org_id
            )
        )
        trial_result = await session.execute(trial_query.where(trials.id == trial_id))
        trial_row = trial_result.mappings().one_or_none()
        if trial_row is None:
            raise HTTPException(status_code=404, detail="Trial not found")
        task_id = str(trial_row["task_id"])
    else:
        task_id_result = await session.execute(
            select(TaskModel.id)
            .join(
                task_experiments,
                and_(
                    task_experiments.c.task_id == TaskModel.id,
                    task_experiments.c.experiment_id == experiment_id,
                    task_experiments.c.deleted_at.is_(None),
                ),
            )
            .where(
                TaskModel.org_id == org_id,
                TaskModel.deleted_at.is_(None),
                or_(
                    TaskModel.id == task_selector,
                    TaskModel.name == task_selector,
                ),
            )
            .order_by(
                case((TaskModel.id == task_selector, 0), else_=1),
                TaskModel.created_at.desc(),
                TaskModel.id.desc(),
            )
            .limit(1)
        )
        task_id = task_id_result.scalar_one_or_none()
        if task_id is None:
            raise HTTPException(status_code=404, detail="Task not found")

    task_result = await session.execute(
        _experiment_task_rows(
            experiment_id=experiment_id,
            org_id=org_id,
            task_ids=[str(task_id)],
            include_user=not _public,
        )
    )
    task_row = task_result.mappings().one_or_none()
    if task_row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    exclusions = (
        await load_cost_exclusions(session)
        if trial_row is not None and _include_cost_exclusion_labels
        else None
    )
    response_type = (
        PublicExperimentFocusResponse if _public else ExperimentFocusResponse
    )
    return response_type(
        revision=experiment["revision"],
        task=_public_task_row(task_row) if _public else _task_row(task_row),
        trial=(
            build_experiment_trial_cell(trial_row, exclusions=exclusions)
            if trial_row is not None
            else None
        ),
    )


async def get_experiment_trial_page_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None,
    limit: int = TRIAL_PAGE_MAX_TRIALS,
    before_created_at: datetime | None = None,
    before_trial_id: str | None = None,
    _experiment: Mapping[str, Any] | None = None,
    _include_cost_exclusion_labels: bool = True,
) -> ExperimentTrialPageResponse:
    if (before_created_at is None) != (before_trial_id is None):
        raise HTTPException(
            status_code=400, detail="Both trial page fields are required"
        )
    if _experiment is None:
        if org_id is None:
            raise ValueError("Member experiment reads require an organization")
        experiment = await _member_experiment(
            session, experiment_id=experiment_id, org_id=org_id
        )
    else:
        experiment = _experiment
    query, trials = _experiment_trial_rows_query(
        experiment_id=experiment_id, org_id=org_id
    )
    if before_created_at is not None:
        query = query.where(
            or_(
                trials.created_at < before_created_at,
                and_(
                    trials.created_at == before_created_at,
                    trials.id < before_trial_id,
                ),
            )
        )
    limit = max(1, min(limit, TRIAL_PAGE_MAX_TRIALS))
    result = await session.execute(
        query.order_by(trials.created_at.desc(), trials.id.desc()).limit(limit + 1)
    )
    rows = list(result.mappings().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    exclusions = (
        await load_cost_exclusions(session)
        if rows and _include_cost_exclusion_labels
        else None
    )
    trials = [build_experiment_trial_cell(row, exclusions=exclusions) for row in rows]
    response = ExperimentTrialPageResponse(
        revision=experiment["revision"], trials=trials
    )
    if has_more and rows:
        response.next_created_at = rows[-1]["created_at"]
        response.next_trial_id = rows[-1]["id"]
    return response


def _public_experiment_identity(experiment: ExperimentModel) -> Mapping[str, Any]:
    return {
        "id": experiment.id,
        "name": experiment.name,
        "created_at": experiment.created_at,
        "revision": (
            experiment.last_activity_at
            or experiment.updated_at
            or experiment.created_at
        ),
    }


async def get_public_experiment_open_core(
    session: AsyncSession,
    *,
    public_token: str,
    limit: int = OPEN_MAX_TASKS,
    before_created_at: datetime | None = None,
    before_task_id: str | None = None,
    include_summary: bool = True,
) -> PublicExperimentOpenResponse:
    experiment = await get_public_experiment(session, public_token)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return await get_experiment_open_core(
        session,
        experiment_id=experiment.id,
        org_id=experiment.org_id,
        limit=limit,
        before_created_at=before_created_at,
        before_task_id=before_task_id,
        include_summary=include_summary,
        _experiment=_public_experiment_identity(experiment),
        _public=True,
    )


async def get_public_experiment_focus_core(
    session: AsyncSession,
    *,
    public_token: str,
    task_selector: str | None = None,
    trial_id: str | None = None,
) -> PublicExperimentFocusResponse:
    experiment = await get_public_experiment(session, public_token)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    response = await get_experiment_focus_core(
        session,
        experiment_id=experiment.id,
        org_id=experiment.org_id,
        task_selector=task_selector,
        trial_id=trial_id,
        _experiment=_public_experiment_identity(experiment),
        _include_cost_exclusion_labels=False,
        _require_grid_trial_visibility=True,
        _public=True,
    )
    if response.trial is not None:
        apply_model_display_names(
            [response.trial], experiment_display_names(experiment)
        )
    return response


async def get_public_experiment_trial_page_core(
    session: AsyncSession,
    *,
    public_token: str,
    limit: int = TRIAL_PAGE_MAX_TRIALS,
    before_created_at: datetime | None = None,
    before_trial_id: str | None = None,
) -> ExperimentTrialPageResponse:
    experiment = await get_public_experiment(session, public_token)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    response = await get_experiment_trial_page_core(
        session,
        experiment_id=experiment.id,
        org_id=experiment.org_id,
        limit=limit,
        before_created_at=before_created_at,
        before_trial_id=before_trial_id,
        _experiment=_public_experiment_identity(experiment),
        _include_cost_exclusion_labels=False,
    )
    apply_model_display_names(response.trials, experiment_display_names(experiment))
    return response

"""Core logic for the ``reports`` primitive.

A *report* is an org-scoped, persistent, optionally-shareable cartesian
grid of ``(task_version × agent)`` that resolves to a trials table on
read. The user authors a YAML/JSON spec (``ReportSpec``); the server
unpacks it into rows across the ``reports`` / ``report_agents`` /
``report_task_versions`` / ``report_source_experiments`` /
``report_cell_pins`` tables on create, and rebuilds it on read.

This module owns:

* spec ↔ DB-row materialization (``serialize_report_to_spec``,
  ``materialize_spec_into_report``, ``build_report_response``)
* dynamic cell resolution (``resolve_report_cells``)
* backfill plan + cost estimation (``compute_backfill_plan``)
* backfill execution (``execute_backfill``) — calls
  ``create_task_sweep_core`` in-process and records an audit row
* public-share token management (``ensure_report_public``)

The auth routers in ``backend/api/routers/reports.py`` and
``backend/api/routers/reports_public.py`` are thin wrappers over these
functions.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.db import (
    ExperimentModel,
    Priority,
    ReportAgentModel,
    ReportBackfillEventModel,
    ReportBackfillEventTrialModel,
    ReportCellPinModel,
    ReportModel,
    ReportSourceExperimentModel,
    ReportTaskVersionModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
)
from oddish.schemas import (
    AgentModelPair,
    BackfillCellPlan,
    BackfillCellSelector,
    BackfillEventResponse,
    BackfillExecuteCellResult,
    BackfillExecuteResponse,
    BackfillPlan,
    HarborConfig,
    PublicReportResponse,
    PublicReportTrialSummary,
    ReportAgentBackfillConfig,
    ReportAgentEntry,
    ReportBackfillSettings,
    ReportColumnResolved,
    ReportListItem,
    ReportResponse,
    ReportSelection,
    ReportShareResponse,
    ReportSourceFilter,
    ReportSpec,
    ReportTaskVersionEntry,
    TaskStatusResponse,
    TaskSweepSubmission,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Look-back window for the historical cost estimate. Older trials are
# excluded so a model's first-month pricing doesn't haunt estimates for
# all time. If the org has no recent data, the query falls back to
# the all-time mean (still org-scoped).
_COST_WINDOW = timedelta(days=30)

# Minimum number of historical trials for an estimate to be considered
# "confident" — surfaced as ``cost_sample_size`` so the UI can dim/warn
# below this threshold.
_COST_MIN_SAMPLE = 5

_TERMINAL_STATUSES: set[str] = {
    TrialStatus.SUCCESS.value,
    TrialStatus.FAILED.value,
}


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------


@dataclass
class _ResolvedRow:
    position: int
    task_version_id: str
    task_id: str
    version: int
    task_name: str
    # The TaskModel (with experiments loaded) — kept so the response
    # builder can hand the same object to ``_build_task_status_response``
    # that the experiment endpoint uses, instead of reinventing trial
    # aggregation. ``None`` only on legacy paths that don't need it.
    task: TaskModel | None = None


@dataclass
class _ResolvedColumn:
    position: int
    column_key: str
    agent: str
    model: str | None


@dataclass
class _CellMatch:
    row_idx: int
    col_idx: int
    column_key: str
    pinned: bool
    have: int
    need: int  # 0 for pinned cells
    trials: list[TrialModel]


# ---------------------------------------------------------------------------
# Spec ↔ DB materialization
# ---------------------------------------------------------------------------


def _normalized_status_values(spec: ReportSpec) -> list[str]:
    return [str(s) for s in spec.source.status]


async def _resolve_task_version_id(
    session: AsyncSession,
    entry: ReportTaskVersionEntry,
    *,
    org_id: str | None,
) -> tuple[str, TaskVersionModel, TaskModel]:
    """Resolve a spec row into a concrete (task_version_id, task_version, task)."""
    if entry.task_version_id:
        result = await session.execute(
            select(TaskVersionModel, TaskModel)
            .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
            .where(TaskVersionModel.id == entry.task_version_id)
        )
        row = result.first()
        if not row:
            raise HTTPException(
                status_code=400,
                detail=f"task_version_id '{entry.task_version_id}' not found",
            )
        task_version, task = row
        if org_id is not None and task.org_id is not None and task.org_id != org_id:
            raise HTTPException(
                status_code=403,
                detail=f"task_version_id '{entry.task_version_id}' is not in your org",
            )
        return task_version.id, task_version, task

    assert entry.task_id is not None  # enforced by ReportTaskVersionEntry validator
    task = await session.get(TaskModel, entry.task_id)
    if not task:
        raise HTTPException(
            status_code=400, detail=f"task_id '{entry.task_id}' not found"
        )
    if org_id is not None and task.org_id is not None and task.org_id != org_id:
        raise HTTPException(
            status_code=403,
            detail=f"task_id '{entry.task_id}' is not in your org",
        )

    if entry.version is None:
        if not task.current_version_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"task '{task.id}' has no current_version; supply an explicit "
                    f"task_versions[].version"
                ),
            )
        task_version = await session.get(TaskVersionModel, task.current_version_id)
        if not task_version:
            raise HTTPException(
                status_code=500,
                detail=f"task '{task.id}' references missing version "
                f"'{task.current_version_id}'",
            )
        return task_version.id, task_version, task

    result = await session.execute(
        select(TaskVersionModel).where(
            TaskVersionModel.task_id == entry.task_id,
            TaskVersionModel.version == entry.version,
        )
    )
    task_version = result.scalar_one_or_none()
    if not task_version:
        raise HTTPException(
            status_code=400,
            detail=f"task '{entry.task_id}' has no version {entry.version}",
        )
    return task_version.id, task_version, task


async def materialize_spec_into_report(
    session: AsyncSession,
    report: ReportModel,
    spec: ReportSpec,
    *,
    org_id: str | None,
) -> None:
    """Unpack a validated spec into the relational child tables on ``report``.

    Caller is responsible for adding ``report`` to the session and
    committing. Replaces any existing child rows: PATCH-with-spec callers
    pass a freshly-cleared report.
    """
    # Parent-level fields
    report.name = spec.name
    report.description = spec.description
    report.spec_version = spec.version
    report.trials_per_cell = spec.trials_per_cell
    report.selection_strategy = spec.selection.strategy
    report.selection_seed = spec.selection.seed
    report.selection_tie_breaker = spec.selection.tie_breaker
    report.source_include_superseded = spec.source.include_superseded
    report.source_status = _normalized_status_values(spec)
    report.backfill_enabled = spec.backfill.enabled
    report.backfill_priority = spec.backfill.priority

    # Wipe child rows so PATCH-with-spec is a clean replace. Use
    # ``awaitable_attrs`` so the implicit lazy load doesn't trigger a
    # MissingGreenlet inside the async session.
    (await report.awaitable_attrs.agents).clear()
    (await report.awaitable_attrs.task_versions).clear()
    (await report.awaitable_attrs.source_experiments).clear()
    (await report.awaitable_attrs.cell_pins).clear()

    # ---- agents (columns) ----------------------------------------
    for idx, agent_entry in enumerate(spec.agents):
        backfill_config_json: dict | None = None
        if agent_entry.backfill is not None:
            backfill_config_json = agent_entry.backfill.model_dump(
                exclude_none=True, mode="json"
            )
            if not backfill_config_json:
                backfill_config_json = None
        report.agents.append(
            ReportAgentModel(
                report_id=report.id,
                position=idx,
                column_key=agent_entry.id,
                agent=agent_entry.agent,
                model=agent_entry.model,
                backfill_config=backfill_config_json,
            )
        )

    # ---- task_versions (rows) ------------------------------------
    # Resolve and dedupe; the unique constraint on
    # (report_id, task_version_id) is also enforced at the DB level.
    seen_tv: set[str] = set()
    for idx, tv_entry in enumerate(spec.task_versions):
        tv_id, _task_version, _task = await _resolve_task_version_id(
            session, tv_entry, org_id=org_id
        )
        if tv_id in seen_tv:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"task_versions[] contains duplicate task_version_id "
                    f"'{tv_id}'"
                ),
            )
        seen_tv.add(tv_id)
        report.task_versions.append(
            ReportTaskVersionModel(
                report_id=report.id,
                position=idx,
                task_version_id=tv_id,
            )
        )

        for column_key, trial_ids in tv_entry.pins.items():
            for pin_idx, trial_id in enumerate(trial_ids):
                report.cell_pins.append(
                    ReportCellPinModel(
                        report_id=report.id,
                        task_version_id=tv_id,
                        column_key=column_key,
                        position=pin_idx,
                        trial_id=trial_id,
                    )
                )

    # ---- source.experiment_ids -----------------------------------
    for experiment_id in spec.source.experiment_ids:
        report.source_experiments.append(
            ReportSourceExperimentModel(
                report_id=report.id,
                experiment_id=experiment_id,
            )
        )


def serialize_report_to_spec(report: ReportModel) -> ReportSpec:
    """Rebuild a ``ReportSpec`` from the relational rows."""
    agents = [
        ReportAgentEntry(
            id=a.column_key,
            agent=a.agent,
            model=a.model,
            backfill=(
                ReportAgentBackfillConfig.model_validate(a.backfill_config)
                if a.backfill_config
                else None
            ),
        )
        for a in sorted(report.agents, key=lambda x: x.position)
    ]

    pins_by_tv: dict[str, dict[str, list[tuple[int, str]]]] = {}
    for pin in report.cell_pins:
        pins_by_tv.setdefault(pin.task_version_id, {}).setdefault(
            pin.column_key, []
        ).append((pin.position, pin.trial_id))

    task_versions: list[ReportTaskVersionEntry] = []
    for tv_row in sorted(report.task_versions, key=lambda x: x.position):
        pins = {
            column_key: [trial_id for _, trial_id in sorted(items)]
            for column_key, items in pins_by_tv.get(tv_row.task_version_id, {}).items()
        }
        task_versions.append(
            ReportTaskVersionEntry(
                task_version_id=tv_row.task_version_id,
                pins=pins,
            )
        )

    return ReportSpec(
        version=report.spec_version,
        name=report.name,
        description=report.description,
        agents=agents,
        task_versions=task_versions,
        trials_per_cell=report.trials_per_cell,
        source=ReportSourceFilter(
            experiment_ids=[s.experiment_id for s in report.source_experiments],
            include_superseded=report.source_include_superseded,
            status=list(report.source_status or []),
        ),
        selection=ReportSelection(
            strategy=report.selection_strategy,  # type: ignore[arg-type]
            seed=report.selection_seed,
            tie_breaker=report.selection_tie_breaker,
        ),
        backfill=ReportBackfillSettings(
            enabled=report.backfill_enabled,
            priority=report.backfill_priority,
        ),
    )


# ---------------------------------------------------------------------------
# Cell resolution
# ---------------------------------------------------------------------------


async def _fetch_rows(
    session: AsyncSession, report: ReportModel
) -> list[_ResolvedRow]:
    """Load the rendered row metadata for a report, in spec order."""
    rows = sorted(report.task_versions, key=lambda x: x.position)
    if not rows:
        return []
    tv_ids = [r.task_version_id for r in rows]
    # ``selectinload(TaskModel.experiments)`` so the downstream
    # ``_build_task_status_response`` call can derive experiment_id /
    # experiment_name from the loaded relationship without firing a
    # lazy load mid-async (which trips MissingGreenlet).
    result = await session.execute(
        select(TaskVersionModel, TaskModel)
        .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
        .options(selectinload(TaskModel.experiments))
        .where(TaskVersionModel.id.in_(tv_ids))
    )
    by_id: dict[str, tuple[TaskVersionModel, TaskModel]] = {
        tv.id: (tv, t) for tv, t in result.all()
    }

    resolved: list[_ResolvedRow] = []
    for r in rows:
        pair = by_id.get(r.task_version_id)
        if not pair:
            # FK is ON DELETE RESTRICT; this should only fire for
            # genuinely-missing rows (data migration accidents).
            continue
        tv, task = pair
        resolved.append(
            _ResolvedRow(
                position=r.position,
                task_version_id=tv.id,
                task_id=task.id,
                version=tv.version,
                task_name=task.name,
                task=task,
            )
        )
    return resolved


def _fetch_columns(report: ReportModel) -> list[_ResolvedColumn]:
    return [
        _ResolvedColumn(
            position=a.position,
            column_key=a.column_key,
            agent=a.agent,
            model=a.model,
        )
        for a in sorted(report.agents, key=lambda x: x.position)
    ]


def _selection_order_clauses(report: ReportModel) -> list[Any]:
    """Build the ORDER BY for cell resolution from ``selection.strategy``.

    All strategies append ``trial_id`` (or the configured tie_breaker) as
    the final sort key so cells are deterministic across reads.
    """
    strategy = report.selection_strategy
    if strategy == "latest":
        clauses: list[Any] = [TrialModel.created_at.desc()]
    elif strategy == "best_reward":
        clauses = [TrialModel.reward.desc().nulls_last(), TrialModel.created_at.desc()]
    elif strategy == "first":
        clauses = [TrialModel.created_at.asc()]
    elif strategy == "random":
        # ORDER BY md5(id || seed) — deterministic per-seed shuffle.
        seed = str(report.selection_seed or 0)
        clauses = [func.md5(TrialModel.id + seed)]
    else:
        # Unknown strategy — fall back to latest.
        clauses = [TrialModel.created_at.desc()]

    tie_breaker = report.selection_tie_breaker or "trial_id"
    if tie_breaker == "trial_id":
        clauses.append(TrialModel.id.asc())
    return clauses


def _trial_match_predicates(
    report: ReportModel,
    *,
    task_version_id: str,
    agent: str,
    model: str | None,
) -> list[Any]:
    preds: list[Any] = [
        TrialModel.task_version_id == task_version_id,
        TrialModel.agent == agent,
    ]
    if model is None:
        preds.append(TrialModel.model.is_(None))
    else:
        preds.append(TrialModel.model == model)

    if report.org_id is not None:
        preds.append(
            or_(TrialModel.org_id == report.org_id, TrialModel.org_id.is_(None))
        )

    if report.source_status:
        preds.append(TrialModel.status.in_(list(report.source_status)))

    if not report.source_include_superseded:
        preds.append(TrialModel.superseded_by_trial_id.is_(None))

    scoped: list[str] = [s.experiment_id for s in report.source_experiments]
    if report.backfill_experiment_id and report.backfill_experiment_id not in scoped:
        scoped = [*scoped, report.backfill_experiment_id]
    if scoped:
        preds.append(TrialModel.experiment_id.in_(scoped))
    return preds


async def _resolve_pinned_cell(
    session: AsyncSession,
    report: ReportModel,
    *,
    task_version_id: str,
    column_key: str,
) -> list[TrialModel]:
    pins = [
        p
        for p in report.cell_pins
        if p.task_version_id == task_version_id and p.column_key == column_key
    ]
    if not pins:
        return []
    pins.sort(key=lambda p: p.position)
    pin_ids = [p.trial_id for p in pins]
    result = await session.execute(
        select(TrialModel).where(TrialModel.id.in_(pin_ids))
    )
    by_id = {t.id: t for t in result.scalars().all()}
    # Preserve pin order, drop any missing trials silently (RESTRICT FK
    # should normally prevent this).
    return [by_id[t] for t in pin_ids if t in by_id]


async def _resolve_cell_trials(
    session: AsyncSession,
    report: ReportModel,
    *,
    row: _ResolvedRow,
    col: _ResolvedColumn,
) -> _CellMatch:
    """Resolve one cell. Pins win over selection queries."""
    pinned_trials = await _resolve_pinned_cell(
        session,
        report,
        task_version_id=row.task_version_id,
        column_key=col.column_key,
    )
    if pinned_trials:
        return _CellMatch(
            row_idx=row.position,
            col_idx=col.position,
            column_key=col.column_key,
            pinned=True,
            have=len(pinned_trials),
            need=0,
            trials=pinned_trials,
        )

    preds = _trial_match_predicates(
        report,
        task_version_id=row.task_version_id,
        agent=col.agent,
        model=col.model,
    )
    order_clauses = _selection_order_clauses(report)

    # Two-step: count first (for the "have" / "need" surface), then
    # fetch up to trials_per_cell ordered trials.
    count_q = select(func.count()).select_from(TrialModel).where(*preds)
    have = int(await session.scalar(count_q) or 0)

    fetch_q = (
        select(TrialModel)
        .where(*preds)
        .order_by(*order_clauses)
        .limit(report.trials_per_cell)
    )
    fetched = list((await session.execute(fetch_q)).scalars().all())
    need = max(0, report.trials_per_cell - have)
    return _CellMatch(
        row_idx=row.position,
        col_idx=col.position,
        column_key=col.column_key,
        pinned=False,
        have=have,
        need=need,
        trials=fetched,
    )


async def _resolve_cell_count_only(
    session: AsyncSession,
    report: ReportModel,
    *,
    row: _ResolvedRow,
    col: _ResolvedColumn,
) -> _CellMatch:
    """Like _resolve_cell_trials but skips the row fetch — used by backfill plan."""
    pinned_trials = await _resolve_pinned_cell(
        session,
        report,
        task_version_id=row.task_version_id,
        column_key=col.column_key,
    )
    if pinned_trials:
        return _CellMatch(
            row_idx=row.position,
            col_idx=col.position,
            column_key=col.column_key,
            pinned=True,
            have=len(pinned_trials),
            need=0,
            trials=[],
        )
    preds = _trial_match_predicates(
        report,
        task_version_id=row.task_version_id,
        agent=col.agent,
        model=col.model,
    )
    have = int(
        await session.scalar(select(func.count()).select_from(TrialModel).where(*preds))
        or 0
    )
    return _CellMatch(
        row_idx=row.position,
        col_idx=col.position,
        column_key=col.column_key,
        pinned=False,
        have=have,
        need=max(0, report.trials_per_cell - have),
        trials=[],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_report_cells(
    session: AsyncSession,
    report: ReportModel,
    *,
    count_only: bool = False,
) -> tuple[list[_ResolvedRow], list[_ResolvedColumn], list[_CellMatch]]:
    """Resolve the rendered grid for a report.

    Returns (rows, columns, cells). ``cells`` covers every (row, col)
    combination; missing cells have ``trials == []`` and ``need > 0``.
    """
    rows = await _fetch_rows(session, report)
    columns = _fetch_columns(report)

    cells: list[_CellMatch] = []
    for row in rows:
        for col in columns:
            if count_only:
                cell = await _resolve_cell_count_only(
                    session, report, row=row, col=col
                )
            else:
                cell = await _resolve_cell_trials(session, report, row=row, col=col)
            cells.append(cell)
    return rows, columns, cells


def _trials_for_row(
    row: _ResolvedRow, cells: Sequence[_CellMatch]
) -> list[TrialModel]:
    return [t for c in cells if c.row_idx == row.position for t in c.trials]


def _row_task_responses(
    rows: Sequence[_ResolvedRow], cells: Sequence[_CellMatch]
) -> list[TaskStatusResponse]:
    """Build a TaskStatusResponse per row, with trials = the cells' trials.

    Reuses the same builders the experiment endpoint uses, so the
    response shape matches ``GET /experiments/{id}/tasks`` and the
    frontend can render reports with the existing
    ``ExperimentDetailView`` and its trials table / chart / drawer.
    """
    from oddish.core.helpers import (  # avoid import-time cycle
        _build_task_status_response,
        build_trial_response,
    )

    out: list[TaskStatusResponse] = []
    for row in rows:
        if row.task is None:
            # _fetch_rows always sets row.task; guard exists to keep
            # mypy happy and to make the failure mode obvious.
            continue
        trials = _trials_for_row(row, cells)
        trial_responses = [
            build_trial_response(t, row.task.task_path) for t in trials
        ]
        total = len(trials)
        completed = sum(
            1 for t in trials if t.status == TrialStatus.SUCCESS
        )
        failed = sum(1 for t in trials if t.status == TrialStatus.FAILED)
        reward_success = sum(1 for t in trials if t.reward == 1)
        reward_sum = sum(t.reward for t in trials if t.reward is not None)
        reward_total = sum(1 for t in trials if t.reward is not None)
        out.append(
            _build_task_status_response(
                row.task,
                total=total,
                completed=completed,
                failed=failed,
                reward_success=reward_success,
                reward_sum=reward_sum,
                reward_total=reward_total,
                include_empty_rewards=True,
                trials=trial_responses,
                effective_version_id=row.task_version_id,
            )
        )
    return out


def _summarize_trial_public(trial: TrialModel) -> PublicReportTrialSummary:
    duration: float | None = None
    if trial.started_at and trial.finished_at:
        duration = (trial.finished_at - trial.started_at).total_seconds()
    return PublicReportTrialSummary(
        id=trial.id,
        agent=trial.agent,
        model=trial.model,
        status=trial.status,
        reward=trial.reward,
        cost_usd=trial.cost_usd,
        started_at=trial.started_at,
        finished_at=trial.finished_at,
        duration_seconds=duration,
        has_trajectory=trial.has_trajectory,
    )


def build_report_response(
    report: ReportModel,
    rows: Sequence[_ResolvedRow],
    columns: Sequence[_ResolvedColumn],
    cells: Sequence[_CellMatch],
) -> ReportResponse:
    """Return the same ``Task[]`` shape the experiment endpoints use.

    Lets the report detail page reuse ``ExperimentDetailView`` /
    ``ExperimentTrialsTable`` / the trial drawer / the pass-at-k chart
    instead of forking the whole render layer.
    """
    spec = serialize_report_to_spec(report)
    resolved_cols = [
        ReportColumnResolved(
            position=c.position,
            column_key=c.column_key,
            agent=c.agent,
            model=c.model,
        )
        for c in columns
    ]
    tasks = _row_task_responses(rows, cells)
    total_trials = sum(len(c.trials) for c in cells)
    total_missing = sum(c.need for c in cells)
    return ReportResponse(
        id=report.id,
        name=report.name,
        description=report.description,
        org_id=report.org_id,
        created_by_user_id=report.created_by_user_id,
        spec=spec,
        is_public=report.is_public,
        public_token=report.public_token,
        backfill_experiment_id=report.backfill_experiment_id,
        created_at=report.created_at,
        updated_at=report.updated_at,
        columns=resolved_cols,
        tasks=tasks,
        total_trials=total_trials,
        total_missing=total_missing,
    )


def build_public_report_response(
    report: ReportModel,
    rows: Sequence[_ResolvedRow],
    columns: Sequence[_ResolvedColumn],
    cells: Sequence[_CellMatch],
    *,
    created_by_display: str | None,
) -> PublicReportResponse:
    resolved_cols = [
        ReportColumnResolved(
            position=c.position,
            column_key=c.column_key,
            agent=c.agent,
            model=c.model,
        )
        for c in columns
    ]
    tasks = _row_task_responses(rows, cells)
    return PublicReportResponse(
        name=report.name,
        description=report.description,
        created_at=report.created_at,
        created_by_display=created_by_display,
        columns=resolved_cols,
        tasks=tasks,
        total_trials=sum(len(c.trials) for c in cells),
    )


def build_report_list_item(report: ReportModel) -> ReportListItem:
    return ReportListItem(
        id=report.id,
        name=report.name,
        description=report.description,
        rows_count=len(report.task_versions),
        columns_count=len(report.agents),
        is_public=report.is_public,
        public_token=report.public_token,
        created_at=report.created_at,
        updated_at=report.updated_at,
    )


def trial_id_set_for_public_access(
    cells: Sequence[_CellMatch],
) -> set[str]:
    """Return the trial-id set reachable through a report's public token.

    Used to gate ``/public/reports/{token}/trials/{trial_id}/...`` deep
    links — the requested trial must appear in the resolved cells.
    """
    return {t.id for c in cells for t in c.trials}


# ---------------------------------------------------------------------------
# Backfill plan + cost estimation
# ---------------------------------------------------------------------------


async def _historical_cost_means(
    session: AsyncSession,
    *,
    org_id: str | None,
    pairs: Iterable[tuple[str, str | None]],
) -> dict[tuple[str, str | None], tuple[float | None, int]]:
    """Return mean ``cost_usd`` and sample size per ``(agent, model)`` pair.

    Tries a 30-day org-scoped window first, then falls back to org-wide
    all-time. Pairs without any data map to ``(None, 0)``.
    """
    pair_list = list({(a, m) for a, m in pairs})
    if not pair_list:
        return {}

    def _query(within_window: bool, scope_org: bool):
        agents = [a for a, _ in pair_list]
        models_with_nulls = [m for _, m in pair_list if m is not None]
        q = (
            select(
                TrialModel.agent,
                TrialModel.model,
                func.avg(TrialModel.cost_usd).label("mean_cost"),
                func.count().label("sample_n"),
            )
            .where(TrialModel.cost_usd.is_not(None))
            .where(TrialModel.status.in_(list(_TERMINAL_STATUSES)))
            .where(TrialModel.agent.in_(agents))
        )
        if models_with_nulls:
            q = q.where(
                or_(
                    TrialModel.model.in_(models_with_nulls),
                    TrialModel.model.is_(None),
                )
            )
        if scope_org and org_id is not None:
            q = q.where(
                or_(TrialModel.org_id == org_id, TrialModel.org_id.is_(None))
            )
        if within_window:
            cutoff = datetime.now(timezone.utc) - _COST_WINDOW
            q = q.where(TrialModel.created_at > cutoff)
        return q.group_by(TrialModel.agent, TrialModel.model)

    results: dict[tuple[str, str | None], tuple[float | None, int]] = {
        pair: (None, 0) for pair in pair_list
    }

    for window, scope in [(True, True), (False, True), (False, False)]:
        rows = (await session.execute(_query(window, scope))).all()
        for agent, model, mean_cost, sample_n in rows:
            key = (agent, model)
            if key in results and results[key][1] == 0:
                results[key] = (
                    float(mean_cost) if mean_cost is not None else None,
                    int(sample_n or 0),
                )
        if all(sample > 0 for _, sample in results.values()):
            break

    return results


async def compute_backfill_plan(
    session: AsyncSession, report: ReportModel
) -> tuple[BackfillPlan, list[_CellMatch]]:
    """Compute the gap + cost estimate.

    Returns the wire ``BackfillPlan`` (suitable for the JSON response and
    for freezing into ``report_backfill_events.plan_snapshot``) along
    with the raw cell-match list (used by execute, never serialized).
    """
    rows, columns, cells = await resolve_report_cells(
        session, report, count_only=True
    )

    rows_by_position = {r.position: r for r in rows}
    cols_by_position = {c.position: c for c in columns}

    needy_cells = [c for c in cells if c.need > 0 and not c.pinned]

    pairs = {
        (cols_by_position[c.col_idx].agent, cols_by_position[c.col_idx].model)
        for c in needy_cells
    }
    means = await _historical_cost_means(session, org_id=report.org_id, pairs=pairs)

    agent_ids_with_config = {
        a.column_key for a in report.agents if a.backfill_config
    }

    cell_plans: list[BackfillCellPlan] = []
    total_est = 0.0
    have_any_cost = False
    total_sample = 0
    columns_missing_config: set[str] = set()

    for cell in needy_cells:
        col = cols_by_position[cell.col_idx]
        row = rows_by_position[cell.row_idx]
        mean_cost, sample_n = means.get((col.agent, col.model), (None, 0))
        est: float | None = (
            float(mean_cost) * cell.need if mean_cost is not None else None
        )
        if est is not None:
            total_est += est
            have_any_cost = True
        total_sample += sample_n

        has_config = col.column_key in agent_ids_with_config
        if not has_config:
            columns_missing_config.add(col.column_key)

        cell_plans.append(
            BackfillCellPlan(
                row_idx=cell.row_idx,
                col_idx=cell.col_idx,
                task_version_id=row.task_version_id,
                task_id=row.task_id,
                column_key=col.column_key,
                agent=col.agent,
                model=col.model,
                have=cell.have,
                need=cell.need,
                est_cost_usd=est,
                cost_sample_size=sample_n,
                has_backfill_config=has_config,
            )
        )

    plan = BackfillPlan(
        total_missing=sum(c.need for c in needy_cells),
        total_est_cost_usd=total_est if have_any_cost else None,
        total_cost_sample_size=total_sample,
        cells=cell_plans,
        columns_missing_backfill_config=sorted(columns_missing_config),
    )
    return plan, cells


# ---------------------------------------------------------------------------
# Backfill execution
# ---------------------------------------------------------------------------


def _build_sweep_submissions(
    report: ReportModel,
    plan: BackfillPlan,
    plan_cells: Sequence[_CellMatch],
    *,
    selected: set[tuple[int, int]] | None,
    backfill_experiment_id: str,
) -> list[TaskSweepSubmission]:
    """Group needy cells by task_version and produce one sweep per row."""
    cols_by_position = {a.position: a for a in report.agents}

    rows_by_position: dict[int, str] = {}
    for cell_plan in plan.cells:
        rows_by_position[cell_plan.row_idx] = cell_plan.task_id

    grouped: dict[str, list[BackfillCellPlan]] = {}
    for cell_plan in plan.cells:
        if cell_plan.need <= 0:
            continue
        if (
            selected is not None
            and (cell_plan.row_idx, cell_plan.col_idx) not in selected
        ):
            continue
        grouped.setdefault(cell_plan.task_id, []).append(cell_plan)

    submissions: list[TaskSweepSubmission] = []
    for task_id, cell_plans in grouped.items():
        configs: list[AgentModelPair] = []
        harbor_for_submission: HarborConfig | None = None
        environment_for_submission: str | None = None
        priority_for_submission: Priority | None = None

        for cell_plan in cell_plans:
            agent_row = cols_by_position.get(cell_plan.col_idx)
            if agent_row is None:
                continue
            cfg_dict = agent_row.backfill_config or {}
            cfg = ReportAgentBackfillConfig.model_validate(cfg_dict) if cfg_dict else (
                ReportAgentBackfillConfig()
            )
            if cfg.harbor is not None and harbor_for_submission is None:
                harbor_for_submission = cfg.harbor
            if cfg.environment is not None and environment_for_submission is None:
                environment_for_submission = cfg.environment
            if cfg.priority is not None and priority_for_submission is None:
                priority_for_submission = cfg.priority

            configs.append(
                AgentModelPair(
                    agent=agent_row.agent,
                    model=agent_row.model,
                    n_trials=cell_plan.need,
                )
            )

        if not configs:
            continue

        submission = TaskSweepSubmission(
            task_id=task_id,
            append_to_task=True,
            configs=configs,
            priority=priority_for_submission or report.backfill_priority,
            experiment_id=backfill_experiment_id,
            tags={"oddish.report_id": report.id},
            environment=environment_for_submission,  # type: ignore[arg-type]
            harbor=harbor_for_submission or HarborConfig(),
        )
        submissions.append(submission)

    return submissions


async def _ensure_backfill_experiment(
    session: AsyncSession, report: ReportModel
) -> ExperimentModel:
    if report.backfill_experiment_id:
        experiment = await session.get(
            ExperimentModel, report.backfill_experiment_id
        )
        if experiment:
            return experiment

    experiment = ExperimentModel(
        name=f"{report.name} [backfill]",
        org_id=report.org_id,
    )
    session.add(experiment)
    await session.flush()
    report.backfill_experiment_id = experiment.id
    return experiment


async def execute_backfill(
    session: AsyncSession,
    report: ReportModel,
    *,
    cells: Sequence[BackfillCellSelector] | None,
    source: str,
    initiated_by_user_id: str | None,
    initiated_by_api_key_id: str | None,
    user_string: str,
    org_id: str | None,
    default_environment: Any = None,
    allowed_environments: Any = None,
) -> BackfillExecuteResponse:
    """Plan + submit + record the audit row in one transaction.

    The audit row is written *before* sweep calls so even a partial
    failure leaves a trace.
    """
    from oddish.core.endpoints import create_task_sweep_core  # avoid circular import

    plan, plan_cells = await compute_backfill_plan(session, report)
    if plan.total_missing == 0:
        raise HTTPException(
            status_code=400, detail="Nothing to backfill — all cells are full"
        )
    if plan.columns_missing_backfill_config:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Some columns require a backfill block in the spec",
                "columns": plan.columns_missing_backfill_config,
            },
        )

    selected: set[tuple[int, int]] | None = None
    if cells is not None:
        selected = {(c.row_idx, c.col_idx) for c in cells}
        missing_in_plan = selected - {
            (c.row_idx, c.col_idx) for c in plan.cells if c.need > 0
        }
        if missing_in_plan:
            raise HTTPException(
                status_code=400,
                detail=f"requested cells are not needy: {sorted(missing_in_plan)}",
            )

    backfill_experiment = await _ensure_backfill_experiment(session, report)

    event = ReportBackfillEventModel(
        report_id=report.id,
        initiated_by_user_id=initiated_by_user_id,
        initiated_by_api_key_id=initiated_by_api_key_id,
        source=source,
        plan_snapshot=plan.model_dump(mode="json"),
        est_cost_usd_total=plan.total_est_cost_usd,
        trials_submitted=0,
        status="submitted",
    )
    session.add(event)
    # Commit the audit row + lazy-created backfill experiment immediately
    # so even a partial sweep failure leaves a trace. After this commit
    # we're back in an implicit transaction for the sweep work.
    await session.commit()

    submissions = _build_sweep_submissions(
        report,
        plan,
        plan_cells,
        selected=selected,
        backfill_experiment_id=backfill_experiment.id,
    )

    by_cell: list[BackfillExecuteCellResult] = []
    new_trial_ids: list[str] = []
    error_msg: str | None = None
    status_value: str = "submitted"

    async def _finalize_event() -> None:
        # Re-fetch the event row through the session so the update lands
        # cleanly even if the prior transaction was rolled back.
        ev = await session.get(ReportBackfillEventModel, event.id)
        if ev is None:
            return
        ev.status = status_value
        ev.error_message = error_msg
        ev.trials_submitted = len(new_trial_ids)
        await session.commit()

    try:
        for submission in submissions:
            submission.user = user_string
            task, new_trials, _is_append, _experiment = await create_task_sweep_core(
                session,
                submission=submission,
                org_id=org_id,
                default_environment=default_environment,
                allowed_environments=allowed_environments,
            )
            await session.flush()
            for trial in new_trials:
                new_trial_ids.append(trial.id)
                session.add(
                    ReportBackfillEventTrialModel(
                        event_id=event.id, trial_id=trial.id
                    )
                )
            for col_pos, count in _trials_by_column(submission, report).items():
                column = next(
                    (a for a in report.agents if a.position == col_pos), None
                )
                if column is None:
                    continue
                row_pos = next(
                    (
                        c.row_idx
                        for c in plan.cells
                        if c.task_id == submission.task_id
                        and c.col_idx == col_pos
                    ),
                    None,
                )
                if row_pos is None:
                    continue
                by_cell.append(
                    BackfillExecuteCellResult(
                        row_idx=row_pos,
                        col_idx=col_pos,
                        column_key=column.column_key,
                        queued=count,
                    )
                )
        await session.commit()
    except HTTPException as exc:
        await session.rollback()
        error_msg = (
            exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        )
        status_value = "failed" if not new_trial_ids else "partial"
        await _finalize_event()
        raise
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        error_msg = str(exc)
        status_value = "failed" if not new_trial_ids else "partial"
        await _finalize_event()
        raise

    await _finalize_event()

    return BackfillExecuteResponse(
        event_id=event.id,
        submitted=len(new_trial_ids),
        total_est_cost_usd=plan.total_est_cost_usd,
        backfill_experiment_id=backfill_experiment.id,
        by_cell=by_cell,
        trial_ids=new_trial_ids,
        status=status_value,  # type: ignore[arg-type]
        error_message=error_msg,
    )


def _trials_by_column(
    submission: TaskSweepSubmission, report: ReportModel
) -> dict[int, int]:
    """Count how many trials each report column contributed to a sweep."""
    by_pair: dict[tuple[str, str | None], int] = {}
    for cfg in submission.configs:
        key = (cfg.agent, cfg.model)
        by_pair[key] = by_pair.get(key, 0) + cfg.n_trials

    out: dict[int, int] = {}
    for col in report.agents:
        n = by_pair.get((col.agent, col.model), 0)
        if n:
            out[col.position] = n
    return out


# ---------------------------------------------------------------------------
# Backfill history (audit log)
# ---------------------------------------------------------------------------


async def list_backfill_events(
    session: AsyncSession,
    report: ReportModel,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[BackfillEventResponse]:
    result = await session.execute(
        select(ReportBackfillEventModel)
        .options(selectinload(ReportBackfillEventModel.trials))
        .where(ReportBackfillEventModel.report_id == report.id)
        .order_by(ReportBackfillEventModel.initiated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    events = result.scalars().all()
    return [
        BackfillEventResponse(
            id=ev.id,
            report_id=ev.report_id,
            initiated_by_user_id=ev.initiated_by_user_id,
            initiated_by_api_key_id=ev.initiated_by_api_key_id,
            initiated_by_display=None,
            source=ev.source,
            initiated_at=ev.initiated_at,
            est_cost_usd_total=ev.est_cost_usd_total,
            trials_submitted=ev.trials_submitted,
            status=ev.status,
            error_message=ev.error_message,
            trial_ids=[t.trial_id for t in ev.trials],
        )
        for ev in events
    ]


# ---------------------------------------------------------------------------
# Public sharing
# ---------------------------------------------------------------------------


def _generate_share_token() -> str:
    return secrets.token_urlsafe(32)


async def ensure_report_public(
    session: AsyncSession, report: ReportModel
) -> None:
    """Mirror of ``ensure_experiment_public`` for reports."""
    if report.is_public and report.public_token:
        return
    if not report.public_token:
        for _ in range(5):
            candidate = _generate_share_token()
            existing = await session.execute(
                select(ReportModel.id).where(ReportModel.public_token == candidate)
            )
            if existing.scalar_one_or_none() is None:
                report.public_token = candidate
                break
        if not report.public_token:
            raise HTTPException(
                status_code=500, detail="Failed to generate unique share token"
            )
    report.is_public = True


def build_share_response(report: ReportModel) -> ReportShareResponse:
    return ReportShareResponse(
        id=report.id,
        name=report.name,
        is_public=report.is_public,
        public_token=report.public_token,
    )


# ---------------------------------------------------------------------------
# Loader helpers used by the routers
# ---------------------------------------------------------------------------


async def get_report_for_org(
    session: AsyncSession, *, report_id: str, org_id: str | None
) -> ReportModel:
    """Authed loader — raises 404 if missing or out-of-org."""
    result = await session.execute(
        select(ReportModel)
        .options(
            selectinload(ReportModel.agents),
            selectinload(ReportModel.task_versions),
            selectinload(ReportModel.source_experiments),
            selectinload(ReportModel.cell_pins),
        )
        .where(ReportModel.id == report_id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if org_id is not None and report.org_id is not None and report.org_id != org_id:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


async def get_public_report(
    session: AsyncSession, public_token: str
) -> ReportModel | None:
    """Anon loader — only returns published reports."""
    result = await session.execute(
        select(ReportModel)
        .options(
            selectinload(ReportModel.agents),
            selectinload(ReportModel.task_versions),
            selectinload(ReportModel.source_experiments),
            selectinload(ReportModel.cell_pins),
        )
        .where(
            ReportModel.public_token == public_token,
            ReportModel.is_public == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


# Small helper so the audit display doesn't leak raw user_ids.
def display_for_user(*, email: str | None, name: str | None) -> str | None:
    if name:
        return name
    if email:
        local = email.split("@", 1)[0]
        return local or email
    return None


# Stable string fingerprint of a spec — useful for cache keys etc. Not
# stored in the DB but exported for future deduplication work.
def spec_fingerprint(spec: ReportSpec) -> str:
    payload = spec.model_dump_json(exclude_none=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

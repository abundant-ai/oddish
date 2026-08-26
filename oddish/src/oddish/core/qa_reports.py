"""Experiment-scoped private QA drafts and public snapshots."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.experiment_membership import trial_in_experiment
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    QAReportItemModel,
    QAReportModel,
    QAReportPublicationModel,
    QAReportTaskModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    task_experiments,
    utcnow,
)
from oddish.schemas import (
    PublicQAReportExperiment,
    PublicQAReportItem,
    PublicQAReportResponse,
    PublicQAReportTask,
    QAReportAvailableItemResponse,
    QAReportCreateRequest,
    QAReportItemPatch,
    QAReportItemResponse,
    QAReportPatchRequest,
    QAReportResponse,
    QAReportTaskPatch,
    QAReportTaskResponse,
)


_SOURCE_PRE_TRIAL = "pre_trial"
_SOURCE_VERDICT = "verdict"
_SOURCE_TRIAL_ANALYSIS = "trial_analysis"


@dataclass(frozen=True)
class _TaskContext:
    task_id: str
    task_version_id: str | None
    task_name: str


@dataclass(frozen=True)
class _Candidate:
    task_id: str
    task_version_id: str | None
    task_name: str
    source_type: str
    source_ref: str
    source_label: str
    source_completed_at: datetime | None
    source_title: str
    source_summary: str | None
    source_recommendation: str | None
    source_evidence: str | None
    title: str
    summary: str | None
    recommendation: str | None
    tier: str | None = None
    dimension: str | None = None
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    outcome: str | None = None
    confidence: str | None = None

    @property
    def candidate_id(self) -> str:
        digest = hashlib.sha256(self.source_ref.encode("utf-8")).hexdigest()[:24]
        return f"qa-{digest}"

    def available_response(self) -> QAReportAvailableItemResponse:
        return QAReportAvailableItemResponse(
            id=self.candidate_id,
            task_id=self.task_id,
            task_version_id=self.task_version_id,
            task_name=self.task_name,
            source_type=self.source_type,  # type: ignore[arg-type]
            source_ref=self.source_ref,
            source_label=self.source_label,
            source_completed_at=self.source_completed_at,
            source_title=self.source_title,
            source_summary=self.source_summary,
            source_recommendation=self.source_recommendation,
            source_evidence=self.source_evidence,
            evidence=self.source_evidence,
            title=self.title,
            summary=self.summary,
            recommendation=self.recommendation,
            tier=self.tier,
            dimension=self.dimension,
            file=self.file,
            line_start=self.line_start,
            line_end=self.line_end,
            outcome=self.outcome,
            confidence=self.confidence,
            internal_note=None,
            include_evidence=False,
        )


def generate_qa_public_token() -> str:
    """Return a dedicated 256-bit, URL-safe QA share token."""

    return secrets.token_urlsafe(32)


async def revoke_public_qa_report_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str | None = None,
) -> bool:
    """Revoke a public QA link before its experiment scope changes.

    Locking the experiment first uses the same lock order as QA publish.
    The immutable publication stays in the database for audit, but a later
    experiment publish cannot make its old QA URL live again.
    """

    experiment_query = select(ExperimentModel.id).where(
        ExperimentModel.id == experiment_id,
        ExperimentModel.deleted_at.is_(None),
    )
    if org_id is not None:
        experiment_query = experiment_query.where(ExperimentModel.org_id == org_id)
    locked_experiment_id = await session.scalar(experiment_query.with_for_update())
    if locked_experiment_id is None:
        return False

    statement = update(QAReportModel).where(
        QAReportModel.experiment_id == experiment_id,
        QAReportModel.deleted_at.is_(None),
        QAReportModel.is_public.is_(True),
    )
    if org_id is not None:
        statement = statement.where(QAReportModel.org_id == org_id)
    result = await session.execute(
        statement.values(
            is_public=False,
            public_token=None,
            updated_at=utcnow(),
        ).execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def revoke_public_qa_reports_for_task_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> int:
    """Revoke every public QA link affected by deleting one shared task."""

    experiment_query = (
        select(ExperimentModel.id)
        .join(
            task_experiments,
            task_experiments.c.experiment_id == ExperimentModel.id,
        )
        .where(
            task_experiments.c.task_id == task_id,
            task_experiments.c.deleted_at.is_(None),
            ExperimentModel.deleted_at.is_(None),
        )
        .order_by(ExperimentModel.id.asc())
    )
    if org_id is not None:
        experiment_query = experiment_query.where(ExperimentModel.org_id == org_id)
    experiment_ids = list((await session.scalars(experiment_query)).all())
    revoked = 0
    for experiment_id in experiment_ids:
        revoked += int(
            await revoke_public_qa_report_core(
                session,
                experiment_id=experiment_id,
                org_id=org_id,
            )
        )
    return revoked


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _short(value: Any, limit: int) -> str | None:
    text = _text(value)
    return text[:limit] if text is not None else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _qa_trial_coverage(trial: TrialModel) -> set[str]:
    """Return the agent trial ids staged in one QA grader's fixed brief."""

    config = trial.harbor_config
    if not isinstance(config, dict):
        return set()
    payload = config.get("analysis_payload")
    if not isinstance(payload, dict):
        return set()
    trial_ids = payload.get("trial_ids")
    if not isinstance(trial_ids, list):
        return set()
    return {value for value in trial_ids if isinstance(value, str) and value}


def _verdict_is_experiment_scoped(
    grader: TrialModel, scoped_trial_ids: set[str]
) -> bool:
    """A task-wide verdict is safe only when every graded trial is in view."""

    coverage = _qa_trial_coverage(grader)
    return bool(coverage) and coverage.issubset(scoped_trial_ids)


def _action_identity(raw: dict, index: int) -> str:
    item_id = _text(raw.get("id"))
    if item_id:
        return hashlib.sha256(f"id:{item_id}".encode()).hexdigest()[:24]
    stable = "|".join(
        str(raw.get(key) or "")
        for key in (
            "source",
            "dimension",
            "problem_type",
            "file",
            "line_start",
            "line_end",
            "title",
        )
    )
    if stable.replace("|", ""):
        return hashlib.sha256(stable.encode()).hexdigest()[:24]
    return f"row-{index}"


def _action_candidate(
    *,
    context: _TaskContext,
    source_type: str,
    source_prefix: str,
    source_label: str,
    completed_at: datetime | None,
    raw: dict,
    index: int,
) -> _Candidate | None:
    title = _text(raw.get("title"))
    if title is None:
        return None
    summary = _text(raw.get("detail")) or _text(raw.get("root_cause"))
    recommendation = _text(raw.get("recommendation"))
    evidence = _text(raw.get("exploit_evidence")) or _text(raw.get("evidence"))
    outcome = "exploited" if raw.get("exploited") is True else None
    identity = _action_identity(raw, index)
    return _Candidate(
        task_id=context.task_id,
        task_version_id=context.task_version_id,
        task_name=context.task_name,
        source_type=source_type,
        source_ref=f"{source_prefix}:action:{identity}",
        source_label=source_label[:255],
        source_completed_at=completed_at,
        source_title=title,
        source_summary=summary,
        source_recommendation=recommendation,
        source_evidence=evidence,
        title=title,
        summary=summary,
        recommendation=recommendation,
        tier=_short(raw.get("tier") or raw.get("severity"), 32),
        dimension=_short(raw.get("dimension"), 64),
        file=_text(raw.get("file")),
        line_start=_integer(raw.get("line_start")),
        line_end=_integer(raw.get("line_end")),
        outcome=outcome,
    )


async def _get_private_experiment(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
    for_update: bool = False,
) -> ExperimentModel:
    statement = select(ExperimentModel).where(
        ExperimentModel.id == experiment_id,
        ExperimentModel.org_id == org_id,
    )
    if for_update:
        statement = statement.with_for_update()
    experiment = await session.scalar(statement)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if experiment.shadow_of is not None:
        raise HTTPException(
            status_code=400,
            detail="A QA report cannot be created for a hidden QA experiment",
        )
    return experiment


async def _get_report(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
    for_update: bool = False,
) -> QAReportModel | None:
    statement = select(QAReportModel).where(
        QAReportModel.experiment_id == experiment_id,
        QAReportModel.org_id == org_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def _task_contexts(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
) -> tuple[list[_TaskContext], dict[str, TaskVersionModel]]:
    tasks = list(
        (
            await session.scalars(
                select(TaskModel)
                .join(
                    task_experiments,
                    task_experiments.c.task_id == TaskModel.id,
                )
                .where(
                    task_experiments.c.experiment_id == experiment_id,
                    task_experiments.c.deleted_at.is_(None),
                    TaskModel.org_id == org_id,
                )
                .order_by(TaskModel.name.asc(), TaskModel.id.asc())
            )
        ).all()
    )
    if not tasks:
        return [], {}

    task_ids = [task.id for task in tasks]
    version_rows = (
        await session.execute(
            select(
                TrialModel.task_id,
                TrialModel.task_version_id,
                TaskVersionModel.version,
            )
            .join(TaskVersionModel, TaskVersionModel.id == TrialModel.task_version_id)
            .where(
                TrialModel.task_id.in_(task_ids),
                TrialModel.org_id == org_id,
                TrialModel.kind == "agent",
                TrialModel.is_probe.is_(False),
                TrialModel.superseded_by_trial_id.is_(None),
                trial_in_experiment(experiment_id),
            )
        )
    ).all()
    represented: dict[str, list[tuple[str, int]]] = {}
    for task_id, version_id, version_number in version_rows:
        if version_id is not None:
            represented.setdefault(str(task_id), []).append(
                (str(version_id), int(version_number))
            )

    contexts: list[_TaskContext] = []
    for task in tasks:
        choices = represented.get(task.id, [])
        represented_ids = {version_id for version_id, _ in choices}
        if task.current_version_id in represented_ids:
            effective = task.current_version_id
        elif choices:
            effective = max(choices, key=lambda row: row[1])[0]
        else:
            effective = task.current_version_id
        contexts.append(
            _TaskContext(
                task_id=task.id,
                task_version_id=effective,
                task_name=task.name,
            )
        )

    ids = [row.task_version_id for row in contexts if row.task_version_id]
    versions = (
        list(
            (
                await session.scalars(
                    select(TaskVersionModel).where(TaskVersionModel.id.in_(ids))
                )
            ).all()
        )
        if ids
        else []
    )
    return contexts, {row.id: row for row in versions}


async def _live_experiment_task_ids(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
) -> set[str]:
    """Return only tasks that are still linked to this experiment."""

    return set(
        (
            await session.scalars(
                select(TaskModel.id)
                .join(
                    task_experiments,
                    task_experiments.c.task_id == TaskModel.id,
                )
                .where(
                    task_experiments.c.experiment_id == experiment_id,
                    task_experiments.c.deleted_at.is_(None),
                    TaskModel.org_id == org_id,
                )
            )
        ).all()
    )


async def _collect_candidates(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
) -> tuple[list[_TaskContext], list[_Candidate]]:
    contexts, versions = await _task_contexts(
        session, experiment_id=experiment_id, org_id=org_id
    )
    if not contexts:
        return [], []

    by_task = {row.task_id: row for row in contexts}
    candidates: list[_Candidate] = []

    # The pre-trial audit is stored on the exact version shown by the
    # experiment. A task's newer default version is never mixed in.
    for context in contexts:
        if context.task_version_id is None:
            continue
        version = versions.get(context.task_version_id)
        raw_items = (
            version.pre_trial.get("items", [])
            if version is not None
            and version.pre_trial_status == VerdictStatus.SUCCESS
            and isinstance(version.pre_trial, dict)
            else []
        )
        if not isinstance(raw_items, list):
            continue
        audit_id = (
            _text(version.pre_trial.get("block_id"))
            if version is not None and isinstance(version.pre_trial, dict)
            else None
        ) or "stored"
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue
            candidate = _action_candidate(
                context=context,
                source_type=_SOURCE_PRE_TRIAL,
                source_prefix=(f"pre_trial:{context.task_version_id}:{audit_id}"),
                source_label="Pre-trial audit",
                completed_at=version.pre_trial_finished_at if version else None,
                raw=raw,
                index=index,
            )
            if candidate is not None:
                candidates.append(candidate)

    effective_filters = [
        and_(
            TrialModel.task_id == row.task_id,
            TrialModel.task_version_id.is_(None)
            if row.task_version_id is None
            else TrialModel.task_version_id == row.task_version_id,
        )
        for row in contexts
    ]
    agent_trials: list[TrialModel] = []
    if effective_filters:
        agent_trials = list(
            (
                await session.scalars(
                    select(TrialModel)
                    .where(
                        TrialModel.org_id == org_id,
                        TrialModel.kind == "agent",
                        TrialModel.is_probe.is_(False),
                        TrialModel.superseded_by_trial_id.is_(None),
                        trial_in_experiment(experiment_id),
                        or_(*effective_filters),
                    )
                    .order_by(TrialModel.created_at.asc(), TrialModel.id.asc())
                )
            ).all()
        )
    scoped_trial_ids_by_task: dict[str, set[str]] = {}
    for trial in agent_trials:
        scoped_trial_ids_by_task.setdefault(trial.task_id, set()).add(trial.id)

    tasks = list(
        (
            await session.scalars(
                select(TaskModel).where(
                    TaskModel.id.in_([row.task_id for row in contexts]),
                    TaskModel.org_id == org_id,
                )
            )
        ).all()
    )

    # QA runs are task-wide, so the grader can live under another experiment's
    # hidden QA shadow when a task is shared. We only trust its fixed coverage
    # below, and never copy another experiment's trial data into this report.
    grader_ids: set[str] = set()
    for trial in agent_trials:
        if isinstance(trial.analysis, dict):
            grader = _text(trial.analysis.get("_graded_by"))
            if grader:
                grader_ids.add(grader)
    for task in tasks:
        if isinstance(task.verdict, dict):
            grader = _text(task.verdict.get("_graded_by"))
            if grader:
                grader_ids.add(grader)

    valid_graders: dict[str, TrialModel] = {}
    if grader_ids:
        grader_rows = list(
            (
                await session.scalars(
                    select(TrialModel).where(
                        TrialModel.id.in_(grader_ids),
                        TrialModel.org_id == org_id,
                        TrialModel.kind == "qa",
                        TrialModel.status == TrialStatus.SUCCESS,
                    )
                )
            ).all()
        )
        valid_graders = {row.id: row for row in grader_rows}

    for trial in agent_trials:
        analysis = trial.analysis
        if (
            not isinstance(analysis, dict)
            or trial.analysis_status != AnalysisStatus.SUCCESS
        ):
            continue
        context = by_task.get(trial.task_id)
        grader_id = _text(analysis.get("_graded_by"))
        grader = valid_graders.get(grader_id or "")
        if (
            context is None
            or grader is None
            or grader.task_id != trial.task_id
            or grader.task_version_id != context.task_version_id
            or trial.id not in _qa_trial_coverage(grader)
        ):
            continue

        classification = _short(analysis.get("classification"), 64)
        subtype = _text(analysis.get("subtype"))
        source_title = subtype or classification or "Trial QA result"
        source_summary = _text(analysis.get("root_cause"))
        recommendation = _text(analysis.get("recommendation"))
        evidence = _text(analysis.get("evidence"))
        label = f"Post-trial QA for {trial.name}"[:255]
        completed_at = trial.analysis_finished_at or grader.finished_at
        candidates.append(
            _Candidate(
                task_id=context.task_id,
                task_version_id=context.task_version_id,
                task_name=context.task_name,
                source_type=_SOURCE_TRIAL_ANALYSIS,
                source_ref=(f"trial_analysis:{trial.id}:{grader.id}:classification"),
                source_label=label,
                source_completed_at=completed_at,
                source_title=source_title,
                source_summary=source_summary,
                source_recommendation=recommendation,
                source_evidence=evidence,
                title=source_title,
                summary=source_summary,
                recommendation=recommendation,
                outcome=classification,
            )
        )
        action_items = analysis.get("action_items", [])
        if not isinstance(action_items, list):
            continue
        for index, raw in enumerate(action_items):
            if not isinstance(raw, dict):
                continue
            candidate = _action_candidate(
                context=context,
                source_type=_SOURCE_TRIAL_ANALYSIS,
                source_prefix=f"trial_analysis:{trial.id}:{grader.id}",
                source_label=label,
                completed_at=completed_at,
                raw=raw,
                index=index,
            )
            if candidate is not None:
                candidates.append(candidate)

    for task in tasks:
        verdict = task.verdict
        context = by_task.get(task.id)
        if (
            not isinstance(verdict, dict)
            or task.verdict_status != VerdictStatus.SUCCESS
            or context is None
        ):
            continue
        grader_id = _text(verdict.get("_graded_by"))
        grader = valid_graders.get(grader_id or "")
        if (
            grader is None
            or grader.task_id != task.id
            or grader.task_version_id != context.task_version_id
            or not _verdict_is_experiment_scoped(
                grader, scoped_trial_ids_by_task.get(task.id, set())
            )
        ):
            continue
        outcome = _short(verdict.get("verdict"), 64)
        source_title = (
            "Task accepted"
            if outcome == "accept"
            else "Task needs changes"
            if outcome == "reject"
            else "Task QA verdict"
        )
        primary_issue = _text(verdict.get("primary_issue"))
        reasoning = _text(verdict.get("reasoning"))
        summary = (
            "\n\n".join(part for part in (primary_issue, reasoning) if part is not None)
            or None
        )
        raw_recommendations = verdict.get("recommendations", [])
        recommendation = (
            "\n".join(
                text
                for value in raw_recommendations
                if (text := _text(value)) is not None
            )
            if isinstance(raw_recommendations, list)
            else None
        )
        candidates.append(
            _Candidate(
                task_id=context.task_id,
                task_version_id=context.task_version_id,
                task_name=context.task_name,
                source_type=_SOURCE_VERDICT,
                source_ref=f"verdict:{task.id}:{grader.id}",
                source_label="Task verdict",
                source_completed_at=task.verdict_finished_at or grader.finished_at,
                source_title=source_title,
                source_summary=summary,
                source_recommendation=recommendation,
                source_evidence=None,
                title=source_title,
                summary=summary,
                recommendation=recommendation,
                outcome=outcome,
                confidence=_short(verdict.get("confidence"), 32),
            )
        )

    candidates.sort(
        key=lambda row: (
            row.task_name.casefold(),
            row.task_id,
            row.source_completed_at.isoformat() if row.source_completed_at else "",
            row.source_ref,
        )
    )
    return contexts, candidates


async def _draft_rows(
    session: AsyncSession, report_id: str
) -> tuple[list[QAReportTaskModel], list[QAReportItemModel]]:
    tasks = list(
        (
            await session.scalars(
                select(QAReportTaskModel)
                .where(QAReportTaskModel.report_id == report_id)
                .order_by(
                    QAReportTaskModel.sort_order.asc(), QAReportTaskModel.id.asc()
                )
            )
        ).all()
    )
    items = list(
        (
            await session.scalars(
                select(QAReportItemModel)
                .where(QAReportItemModel.report_id == report_id)
                .order_by(
                    QAReportItemModel.sort_order.asc(), QAReportItemModel.id.asc()
                )
            )
        ).all()
    )
    return tasks, items


def _item_response(row: QAReportItemModel) -> QAReportItemResponse:
    return QAReportItemResponse(
        id=row.id,
        source_type=row.source_type,  # type: ignore[arg-type]
        source_ref=row.source_ref,
        source_label=row.source_label,
        source_completed_at=row.source_completed_at,
        source_title=row.source_title,
        source_summary=row.source_summary,
        source_recommendation=row.source_recommendation,
        source_evidence=row.source_evidence,
        # Private curation always receives its saved evidence. The visibility
        # choice applies only in preview and the public snapshot.
        evidence=row.evidence,
        is_visible=row.is_visible,
        include_evidence=row.include_evidence,
        sort_order=row.sort_order,
        title=row.title,
        summary=row.summary,
        recommendation=row.recommendation,
        customer_note=row.customer_note,
        internal_note=row.internal_note,
        tier=row.tier,
        dimension=row.dimension,
        file=row.file,
        line_start=row.line_start,
        line_end=row.line_end,
        outcome=row.outcome,
        confidence=row.confidence,
    )


async def _selected_publication(
    session: AsyncSession, report: QAReportModel
) -> QAReportPublicationModel | None:
    if report.published_snapshot_id is None:
        return None
    # Both keys are required. A corrupt pointer must never make one report
    # serve another report's snapshot.
    return await session.scalar(
        select(QAReportPublicationModel).where(
            QAReportPublicationModel.id == report.published_snapshot_id,
            QAReportPublicationModel.report_id == report.id,
        )
    )


async def _report_response(
    session: AsyncSession,
    *,
    report: QAReportModel,
    collect_available: bool = True,
) -> QAReportResponse:
    task_rows, item_rows = await _draft_rows(session, report.id)
    items_by_task: dict[str, list[QAReportItemResponse]] = {}
    for item in item_rows:
        items_by_task.setdefault(item.report_task_id, []).append(_item_response(item))
    tasks = [
        QAReportTaskResponse(
            id=row.id,
            task_id=row.task_id,
            task_version_id=row.task_version_id,
            name=row.name,
            summary=row.summary,
            internal_note=row.internal_note,
            is_visible=row.is_visible,
            sort_order=row.sort_order,
            items=items_by_task.get(row.id, []),
        )
        for row in task_rows
    ]

    available: list[QAReportAvailableItemResponse] = []
    if collect_available:
        _contexts, candidates = await _collect_candidates(
            session,
            experiment_id=report.experiment_id,
            org_id=report.org_id,
        )
        known = {row.source_ref for row in item_rows}
        available = [
            row.available_response()
            for row in candidates
            if row.source_ref not in known
        ]

    publication = await _selected_publication(session, report)
    live_publication = publication if report.is_public else None
    live_task_ids = await _live_experiment_task_ids(
        session,
        experiment_id=report.experiment_id,
        org_id=report.org_id,
    )
    scope_stale = any(
        row.is_visible and row.task_id not in live_task_ids for row in task_rows
    )
    current_scope_task_ids = [
        row.task_id
        for row in task_rows
        if row.is_visible and row.task_id in live_task_ids
    ]
    published_scope_matches = (
        publication is not None
        and isinstance(publication.scope_task_ids, list)
        and publication.scope_task_ids == current_scope_task_ids
    )
    return QAReportResponse(
        id=report.id,
        experiment_id=report.experiment_id,
        title=report.title,
        summary=report.summary or "",
        conclusion=report.conclusion or "",
        customer_note=report.customer_note,
        internal_note=report.internal_note,
        draft_version=report.draft_version,
        is_public=report.is_public,
        public_token=report.public_token if report.is_public else None,
        published_at=(live_publication.published_at if live_publication else None),
        has_unpublished_changes=(
            not report.is_public
            or publication is None
            or publication.draft_version != report.draft_version
            or not published_scope_matches
        ),
        scope_stale=scope_stale,
        tasks=tasks,
        available_items=available,
        new_item_count=len(available),
        created_at=report.created_at,
        updated_at=report.updated_at,
    )


async def get_qa_report_core(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> QAReportResponse:
    await _get_private_experiment(session, experiment_id=experiment_id, org_id=org_id)
    report = await _get_report(session, experiment_id=experiment_id, org_id=org_id)
    if report is None:
        raise HTTPException(status_code=404, detail="QA report not found")
    return await _report_response(session, report=report)


async def _sync_report(
    session: AsyncSession,
    *,
    report: QAReportModel,
    bump_draft: bool,
) -> int:
    contexts, candidates = await _collect_candidates(
        session,
        experiment_id=report.experiment_id,
        org_id=report.org_id,
    )
    task_rows, item_rows = await _draft_rows(session, report.id)
    tasks_by_id = {row.task_id: row for row in task_rows}
    known = {row.source_ref for row in item_rows}
    live_task_ids = {row.task_id for row in contexts}
    next_task_order = max((row.sort_order for row in task_rows), default=-1) + 1
    next_item_order = {
        row.id: max(
            (item.sort_order for item in item_rows if item.report_task_id == row.id),
            default=-1,
        )
        + 1
        for row in task_rows
    }

    # Creation includes every experiment task. This gives the private editor a
    # clear empty state when a task has no finished QA yet.
    for context in contexts:
        if context.task_id in tasks_by_id:
            continue
        row = QAReportTaskModel(
            report_id=report.id,
            task_id=context.task_id,
            task_version_id=context.task_version_id,
            name=context.task_name,
            sort_order=next_task_order,
        )
        next_task_order += 1
        session.add(row)
        await session.flush()
        tasks_by_id[context.task_id] = row
        next_item_order[row.id] = 0

    added = 0
    for candidate in candidates:
        if candidate.source_ref in known:
            continue
        task_row = tasks_by_id.get(candidate.task_id)
        if task_row is None:
            continue
        order = next_item_order.get(task_row.id, 0)
        session.add(
            QAReportItemModel(
                report_id=report.id,
                report_task_id=task_row.id,
                source_type=candidate.source_type,
                source_ref=candidate.source_ref,
                source_label=candidate.source_label,
                source_completed_at=candidate.source_completed_at,
                source_title=candidate.source_title,
                source_summary=candidate.source_summary,
                source_recommendation=candidate.source_recommendation,
                source_evidence=candidate.source_evidence,
                is_visible=False,
                include_evidence=False,
                title=candidate.title,
                summary=candidate.summary,
                recommendation=candidate.recommendation,
                evidence=candidate.source_evidence,
                tier=candidate.tier,
                dimension=candidate.dimension,
                file=candidate.file,
                line_start=candidate.line_start,
                line_end=candidate.line_end,
                outcome=candidate.outcome,
                confidence=candidate.confidence,
                sort_order=order,
            )
        )
        next_item_order[task_row.id] = order + 1
        known.add(candidate.source_ref)
        added += 1

    hid_removed_task = False
    for row in task_rows:
        if row.task_id not in live_task_ids and row.is_visible:
            row.is_visible = False
            row.updated_at = utcnow()
            hid_removed_task = True

    changed = added > 0 or len(tasks_by_id) > len(task_rows) or hid_removed_task
    if changed and bump_draft:
        report.draft_version += 1
        report.updated_at = utcnow()
    if changed:
        await session.flush()
    return added


async def create_qa_report_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
    created_by_user_id: str | None,
    payload: QAReportCreateRequest | None = None,
) -> QAReportResponse:
    await _get_private_experiment(
        session,
        experiment_id=experiment_id,
        org_id=org_id,
        # A missing report row cannot be locked. Lock its parent so concurrent
        # Create QA calls serialize before the unique insert.
        for_update=True,
    )
    existing = await _get_report(
        session, experiment_id=experiment_id, org_id=org_id, for_update=True
    )
    if existing is not None:
        return await _report_response(session, report=existing)

    payload = payload or QAReportCreateRequest()
    report = QAReportModel(
        org_id=org_id,
        experiment_id=experiment_id,
        created_by_user_id=created_by_user_id,
        title=payload.title or "QA",
        summary=payload.summary,
        conclusion=payload.conclusion,
        customer_note=payload.customer_note,
        internal_note=payload.internal_note,
    )
    session.add(report)
    await session.flush()
    await _sync_report(session, report=report, bump_draft=False)
    return await _report_response(session, report=report)


async def sync_qa_report_core(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> QAReportResponse:
    await _get_private_experiment(session, experiment_id=experiment_id, org_id=org_id)
    report = await _get_report(
        session, experiment_id=experiment_id, org_id=org_id, for_update=True
    )
    if report is None:
        raise HTTPException(status_code=404, detail="QA report not found")
    await _sync_report(session, report=report, bump_draft=True)
    return await _report_response(session, report=report)


def _set_text_field(row: Any, field: str, value: str | None) -> bool:
    cleaned = _text(value)
    if getattr(row, field) == cleaned:
        return False
    setattr(row, field, cleaned)
    return True


def _apply_task_patch(row: QAReportTaskModel, patch: QAReportTaskPatch) -> bool:
    changed = False
    for field in ("name", "summary", "internal_note"):
        if field in patch.model_fields_set:
            changed = _set_text_field(row, field, getattr(patch, field)) or changed
    for field in ("is_visible", "sort_order"):
        value = getattr(patch, field)
        if field in patch.model_fields_set and value is not None:
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed = True
    return changed


def _apply_item_patch(row: QAReportItemModel, patch: QAReportItemPatch) -> bool:
    changed = False
    for field in (
        "title",
        "summary",
        "recommendation",
        "evidence",
        "customer_note",
        "internal_note",
    ):
        if field in patch.model_fields_set:
            changed = _set_text_field(row, field, getattr(patch, field)) or changed
    for field in ("include_evidence", "is_visible", "sort_order"):
        value = getattr(patch, field)
        if field in patch.model_fields_set and value is not None:
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed = True
    return changed


async def patch_qa_report_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
    payload: QAReportPatchRequest,
) -> QAReportResponse:
    await _get_private_experiment(session, experiment_id=experiment_id, org_id=org_id)
    report = await _get_report(
        session, experiment_id=experiment_id, org_id=org_id, for_update=True
    )
    if report is None:
        raise HTTPException(status_code=404, detail="QA report not found")
    if payload.expected_draft_version != report.draft_version:
        raise HTTPException(status_code=409, detail="The QA draft changed. Reload it.")

    task_rows, item_rows = await _draft_rows(session, report.id)
    tasks_by_id = {row.id: row for row in task_rows}
    items_by_id = {row.id: row for row in item_rows}
    missing_tasks = [row.id for row in payload.tasks if row.id not in tasks_by_id]
    missing_items = [row.id for row in payload.items if row.id not in items_by_id]
    if missing_tasks or missing_items:
        raise HTTPException(status_code=404, detail="QA draft row not found")

    changed = False
    for field in (
        "title",
        "summary",
        "conclusion",
        "customer_note",
        "internal_note",
    ):
        if field in payload.model_fields_set:
            changed = _set_text_field(report, field, getattr(payload, field)) or changed
    for patch in payload.tasks:
        changed = _apply_task_patch(tasks_by_id[patch.id], patch) or changed
    for patch in payload.items:
        changed = _apply_item_patch(items_by_id[patch.id], patch) or changed

    if changed:
        report.draft_version += 1
        report.updated_at = utcnow()
        await session.flush()
    return await _report_response(session, report=report)


def _public_item(row: QAReportItemModel) -> PublicQAReportItem:
    include_evidence = row.include_evidence
    return PublicQAReportItem(
        source_type=row.source_type,  # type: ignore[arg-type]
        title=row.title,
        summary=row.summary,
        recommendation=row.recommendation,
        customer_note=row.customer_note,
        evidence=row.evidence if include_evidence else None,
        tier=row.tier,
        dimension=row.dimension,
        file=row.file if include_evidence else None,
        line_start=row.line_start if include_evidence else None,
        line_end=row.line_end if include_evidence else None,
        outcome=row.outcome,
        confidence=row.confidence,
    )


async def _build_public_snapshot(
    session: AsyncSession,
    *,
    report: QAReportModel,
    experiment: ExperimentModel,
    published_at: datetime,
    live_task_ids: set[str],
) -> tuple[PublicQAReportResponse, list[str]]:
    task_rows, item_rows = await _draft_rows(session, report.id)
    visible_items: dict[str, list[PublicQAReportItem]] = {}
    for item in item_rows:
        if item.is_visible:
            visible_items.setdefault(item.report_task_id, []).append(_public_item(item))
    included_rows = [
        row for row in task_rows if row.is_visible and row.task_id in live_task_ids
    ]
    tasks = [
        PublicQAReportTask(
            name=row.name,
            summary=row.summary,
            items=visible_items.get(row.id, []),
        )
        for row in included_rows
    ]
    return (
        PublicQAReportResponse(
            title=report.title,
            summary=report.summary or "",
            conclusion=report.conclusion or "",
            customer_note=report.customer_note,
            published_at=published_at,
            experiment=PublicQAReportExperiment(
                name=experiment.name,
                description=experiment.description,
            ),
            tasks=tasks,
        ),
        [row.task_id for row in included_rows],
    )


async def _mint_unique_token(session: AsyncSession) -> str:
    for _ in range(5):
        candidate = generate_qa_public_token()
        exists = await session.scalar(
            select(QAReportModel.id).where(QAReportModel.public_token == candidate)
        )
        if exists is None:
            return candidate
    raise HTTPException(status_code=500, detail="Could not create a QA share link")


async def publish_qa_report_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
    published_by_user_id: str | None,
    expected_draft_version: int,
    expected_public_token: str | None,
) -> QAReportResponse:
    experiment = await _get_private_experiment(
        session,
        experiment_id=experiment_id,
        org_id=org_id,
        for_update=True,
    )
    if not experiment.is_public or experiment.public_token is None:
        raise HTTPException(
            status_code=409,
            detail="Publish the experiment before you publish its QA report",
        )
    report = await _get_report(
        session, experiment_id=experiment_id, org_id=org_id, for_update=True
    )
    if report is None:
        raise HTTPException(status_code=404, detail="QA report not found")
    if report.draft_version != expected_draft_version:
        raise HTTPException(status_code=409, detail="The QA draft changed. Reload it.")
    public_token_matches = (
        report.public_token is None
        if expected_public_token is None
        else report.public_token is not None
        and secrets.compare_digest(report.public_token, expected_public_token)
    )
    if not public_token_matches:
        raise HTTPException(
            status_code=409, detail="The public QA link changed. Reload it."
        )
    live_task_ids = await _live_experiment_task_ids(
        session, experiment_id=experiment_id, org_id=org_id
    )
    task_rows, _item_rows = await _draft_rows(session, report.id)
    stale_visible_tasks = [
        row.name
        for row in task_rows
        if row.is_visible and row.task_id not in live_task_ids
    ]
    if stale_visible_tasks:
        raise HTTPException(
            status_code=409,
            detail="The experiment task list changed. Sync and review QA first.",
        )
    scope_task_ids = [
        row.task_id
        for row in task_rows
        if row.is_visible and row.task_id in live_task_ids
    ]
    current = await _selected_publication(session, report)
    if (
        report.is_public
        and report.public_token is not None
        and current is not None
        and current.draft_version == report.draft_version
        and current.scope_task_ids == scope_task_ids
    ):
        return await _report_response(session, report=report)

    published_at = utcnow()
    snapshot, built_scope_task_ids = await _build_public_snapshot(
        session,
        report=report,
        experiment=experiment,
        published_at=published_at,
        live_task_ids=live_task_ids,
    )
    publication = QAReportPublicationModel(
        report_id=report.id,
        draft_version=report.draft_version,
        snapshot=snapshot.model_dump(mode="json"),
        scope_task_ids=built_scope_task_ids,
        published_by_user_id=published_by_user_id,
        published_at=published_at,
    )
    session.add(publication)
    await session.flush()
    if report.public_token is None:
        report.public_token = await _mint_unique_token(session)
    report.published_snapshot_id = publication.id
    report.is_public = True
    await session.flush()
    return await _report_response(session, report=report)


async def preview_qa_report_core(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> PublicQAReportResponse:
    """Build a private preview with the same allow-list as publication."""

    experiment = await _get_private_experiment(
        session, experiment_id=experiment_id, org_id=org_id
    )
    report = await _get_report(session, experiment_id=experiment_id, org_id=org_id)
    if report is None:
        raise HTTPException(status_code=404, detail="QA report not found")
    live_task_ids = await _live_experiment_task_ids(
        session, experiment_id=experiment_id, org_id=org_id
    )
    snapshot, _scope_task_ids = await _build_public_snapshot(
        session,
        report=report,
        experiment=experiment,
        published_at=utcnow(),
        live_task_ids=live_task_ids,
    )
    return snapshot


async def unpublish_qa_report_core(
    session: AsyncSession,
    *,
    experiment_id: str,
    org_id: str,
    expected_draft_version: int,
    expected_public_token: str,
) -> QAReportResponse:
    await _get_private_experiment(session, experiment_id=experiment_id, org_id=org_id)
    report = await _get_report(
        session, experiment_id=experiment_id, org_id=org_id, for_update=True
    )
    if report is None:
        raise HTTPException(status_code=404, detail="QA report not found")
    if report.draft_version != expected_draft_version:
        raise HTTPException(status_code=409, detail="The QA draft changed. Reload it.")
    if report.public_token is None or not secrets.compare_digest(
        report.public_token, expected_public_token
    ):
        raise HTTPException(
            status_code=409, detail="The public QA link changed. Reload it."
        )
    report.is_public = False
    # Clearing the token revokes the old URL. The next publish must mint a new
    # token; the old immutable snapshot remains as audit history.
    report.public_token = None
    await session.flush()
    return await _report_response(session, report=report)


async def get_public_qa_report_core(
    session: AsyncSession,
    *,
    experiment_token: str,
    qa_token: str,
) -> PublicQAReportResponse | None:
    row = (
        await session.execute(
            select(
                QAReportPublicationModel,
                QAReportModel.experiment_id,
                QAReportModel.org_id,
            )
            .join(
                QAReportModel,
                and_(
                    QAReportModel.published_snapshot_id == QAReportPublicationModel.id,
                    QAReportModel.id == QAReportPublicationModel.report_id,
                ),
            )
            .join(
                ExperimentModel,
                ExperimentModel.id == QAReportModel.experiment_id,
            )
            .where(
                ExperimentModel.deleted_at.is_(None),
                ExperimentModel.is_public.is_(True),
                ExperimentModel.public_token == experiment_token,
                QAReportModel.deleted_at.is_(None),
                QAReportModel.org_id == ExperimentModel.org_id,
                QAReportModel.is_public.is_(True),
                QAReportModel.public_token == qa_token,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    publication, experiment_id, org_id = row
    if not isinstance(publication.snapshot, dict):
        return None
    if not await _publication_scope_is_live(
        session,
        publication=publication,
        experiment_id=experiment_id,
        org_id=org_id,
    ):
        return None
    return PublicQAReportResponse.model_validate(publication.snapshot)


async def _publication_scope_is_live(
    session: AsyncSession,
    *,
    publication: QAReportPublicationModel,
    experiment_id: str,
    org_id: str,
) -> bool:
    """Confirm that every task saved in a publication is still in scope."""

    scope_task_ids = publication.scope_task_ids
    if not isinstance(scope_task_ids, list) or not all(
        isinstance(task_id, str) for task_id in scope_task_ids
    ):
        return False
    live_task_ids = await _live_experiment_task_ids(
        session,
        experiment_id=experiment_id,
        org_id=org_id,
    )
    return set(scope_task_ids).issubset(live_task_ids)


async def get_public_qa_token_for_experiment(
    session: AsyncSession, *, experiment_id: str
) -> str | None:
    row = (
        await session.execute(
            select(
                QAReportModel.public_token,
                QAReportModel.org_id,
                QAReportPublicationModel,
            )
            .join(
                QAReportPublicationModel,
                and_(
                    QAReportPublicationModel.id == QAReportModel.published_snapshot_id,
                    QAReportPublicationModel.report_id == QAReportModel.id,
                ),
            )
            .join(
                ExperimentModel,
                ExperimentModel.id == QAReportModel.experiment_id,
            )
            .where(
                QAReportModel.experiment_id == experiment_id,
                QAReportModel.deleted_at.is_(None),
                QAReportModel.is_public.is_(True),
                QAReportModel.public_token.is_not(None),
                ExperimentModel.deleted_at.is_(None),
                ExperimentModel.is_public.is_(True),
                ExperimentModel.public_token.is_not(None),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    public_token, org_id, publication = row
    if not await _publication_scope_is_live(
        session,
        publication=publication,
        experiment_id=experiment_id,
        org_id=org_id,
    ):
        return None
    return public_token

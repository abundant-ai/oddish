"""Report generation worker job: gather trials -> ensure analysis -> run core -> persist.

Thin adapter over oddish.evals.report.core.run_report_eval (pure). Mirrors
qa_handler: sets domain status on the ReportModel; the worker_jobs row status is
written by the dispatcher based on the JobOutcome the handler returns.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import or_, select

from oddish.core.report_inputs import build_report_inputs
from oddish.core.reports import experiment_ids_for_report
from oddish.core.experiment_membership import trial_in_experiment
from oddish.db import get_session
from oddish.db.models import (
    AnalysisStatus,
    JobStatus,
    ReportModel,
    TaskModel,
    TrialModel,
    utcnow,
)
from oddish.evals.report.core import run_report_eval
from oddish.evals.report.schemas import ReportEvalConfig
from oddish.workers.queue.analysis_handler import classify_trial_and_store


async def _gather_trial_rows(session, report_id: str, org_id: str | None):
    exp_ids = await experiment_ids_for_report(session, report_id)
    if not exp_ids:
        return []
    clauses = [trial_in_experiment(eid) for eid in exp_ids]
    stmt = (
        select(TrialModel, TaskModel.task_path)
        .join(TaskModel, TrialModel.task_id == TaskModel.id)
        .where(
            or_(*clauses),
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.org_id == org_id,
        )
    )
    rows = (await session.execute(stmt)).all()
    # De-dupe trials gathered via multiple experiments.
    seen: set[str] = set()
    out = []
    for trial, task_path in rows:
        if trial.id in seen:
            continue
        seen.add(trial.id)
        out.append((trial, task_path))
    return out


async def run_report_generation_job(
    report_id: str, *, worker_job_id: str | None = None
) -> None:
    # 1. Load + set RUNNING.
    async with get_session() as session:
        report = await session.get(ReportModel, report_id, with_for_update=True)
        if report is None:
            return
        if report.status in (JobStatus.SUCCESS, JobStatus.FAILED):
            return
        report.status = JobStatus.RUNNING
        report.started_at = utcnow()
        org_id = report.org_id

    # 2. Ensure each trial has analysis (best-effort), then gather again fresh.
    async with get_session() as session:
        rows = await _gather_trial_rows(session, report_id, org_id)
        trial_ids = [t.id for t, _ in rows]

    for tid in trial_ids:
        async with get_session() as session:
            trial = await session.get(TrialModel, tid)
            needs = trial is not None and trial.analysis_status not in (
                AnalysisStatus.SUCCESS, AnalysisStatus.FAILED
            )
        if needs:
            try:
                await classify_trial_and_store(tid)
            except Exception:
                pass  # skip un-analyzable trials; they still count toward num_trials

    # 3. Build inputs + run the pure core.
    try:
        async with get_session() as session:
            rows = await _gather_trial_rows(session, report_id, org_id)
            inputs = await build_report_inputs(rows)
        output = await run_report_eval(inputs, ReportEvalConfig())
        error = None
    except Exception as exc:  # noqa: BLE001
        output = None
        error = f"Report generation failed: {exc}"

    # 4. Persist under shield so a cancel mid-write can't corrupt state.
    async def _store() -> None:
        async with get_session() as session:
            report = await session.get(ReportModel, report_id, with_for_update=True)
            if report is None:
                return
            if output is not None:
                report.bad_failure_content = output.sections["bad"]
                report.good_failure_content = output.sections["good"]
                report.universal_capabilities_content = output.sections["capabilities"]
                report.headroom_analysis = output.sections["headroom"]
                report.num_trials = output.counts["trials"]
                report.num_bad_failures = output.counts["bad"]
                report.num_good_failures = output.counts["good"]
                report.breakdown = output.breakdown
                report.status = JobStatus.SUCCESS
                report.error = None
            else:
                report.status = JobStatus.FAILED
                report.error = error
            report.finished_at = utcnow()

    await asyncio.shield(_store())


async def _worker_report_status(report_id: str) -> JobStatus | None:
    async with get_session() as session:
        report = await session.get(ReportModel, report_id)
        return None if report is None else report.status

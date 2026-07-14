from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from oddish.config import settings
from oddish.core.baseline_gate import GATE_SKIP_PREFIX
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
    utcnow,
)
from oddish.workers.queue.analysis_handler import classify_trial_and_store
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

QA_HEARTBEAT_INTERVAL_SECONDS = 30


async def _heartbeat_qa_worker_job(
    *,
    worker_job_id: str,
    stop_event: asyncio.Event,
) -> None:
    """Keep ``worker_jobs.heartbeat_at`` fresh during the QA job."""
    consecutive_failures = 0
    pending_failure_count = 0
    pending_last_error: str | None = None

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=QA_HEARTBEAT_INTERVAL_SECONDS
            )
        except TimeoutError:
            pass

        if stop_event.is_set():
            return

        try:
            await heartbeat_worker_job(
                worker_job_id,
                pending_failure_count=pending_failure_count,
                pending_last_error=pending_last_error,
            )
            if consecutive_failures > 0:
                console.print(
                    f"[green]QA worker_job {worker_job_id} heartbeat "
                    f"recovered after {consecutive_failures} failure(s)[/green]"
                )
            consecutive_failures = 0
            pending_failure_count = 0
            pending_last_error = None
        except Exception as exc:
            consecutive_failures += 1
            pending_failure_count += 1
            pending_last_error = f"{type(exc).__name__}: {exc}"


async def _worker_job_is_running(
    session,
    worker_job_id: str | None,
    *,
    with_for_update: bool = False,
) -> bool:
    if not worker_job_id:
        return True
    statement = select(WorkerJobModel.status).where(WorkerJobModel.id == worker_job_id)
    if with_for_update:
        statement = statement.with_for_update()
    status = await session.scalar(statement)
    return status == WorkerJobStatus.RUNNING


def _trial_needs_classification(analysis_status: AnalysisStatus | None) -> bool:
    """Return true when a live trial still needs QA."""
    return analysis_status not in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED)


def _classifications_from_trials(trials) -> list:
    """Build verdict inputs from stored trial QA."""
    from oddish.analyze import Classification, TrialClassification

    classifications: list = []
    for trial in trials:
        analysis = trial.analysis
        if (
            trial.analysis_status == AnalysisStatus.SUCCESS
            and isinstance(analysis, dict)
            and "classification" in analysis
        ):
            classifications.append(
                TrialClassification(
                    trial_name=analysis.get("trial_name", trial.id),
                    classification=Classification(analysis["classification"]),
                    subtype=analysis.get("subtype", "Unknown"),
                    evidence=analysis.get("evidence", ""),
                    root_cause=analysis.get("root_cause", ""),
                    recommendation=analysis.get("recommendation", ""),
                    reward=analysis.get("reward"),
                )
            )
    return classifications


async def _load_live_trials_for_classification(
    task_id: str,
) -> list[tuple[str, AnalysisStatus | None]]:
    """Return live trial IDs and QA states.

    Trials created by the Sauron->Oddish bulk migration (``imported_at IS NOT
    NULL``) are excluded: classifying ~1M historical trials was ruled out on
    cost. This deliberately diverges from small ad-hoc ``oddish import`` rows
    (``origin='imported'`` but ``imported_at IS NULL``), which keep the stock
    join-the-QA-job behavior. Migrated trials can still be analyzed manually
    via backfill_analysis.py.
    """
    async with get_session() as session:
        rows = (
            await session.execute(
                select(
                    TrialModel.id,
                    TrialModel.analysis_status,
                ).where(
                    TrialModel.task_id == task_id,
                    TrialModel.superseded_by_trial_id.is_(None),
                    # Exclude bulk-migrated Sauron trials (see docstring): too
                    # costly to classify ~1M historical rows.
                    TrialModel.imported_at.is_(None),
                    # Gate-skipped trials never ran (no logs to classify); a
                    # classifier run on them would emit phantom failures and
                    # pollute the verdict + the agent's pass/fail metrics. New
                    # skipped trials carry status=SKIPPED; the legacy prefix
                    # check still excludes pre-SKIPPED rows (marked FAILED +
                    # the old sentinel message, since we don't backfill).
                    TrialModel.status != TrialStatus.SKIPPED,
                    func.coalesce(TrialModel.error_message, "").notlike(
                        f"{GATE_SKIP_PREFIX}%"
                    ),
                )
            )
        ).all()

    return [(str(trial_id), analysis_status) for trial_id, analysis_status in rows]


async def run_task_qa_job(
    task_id: str,
    queue_key: str,
    modal_function_call_id: str | None = None,
    worker_job_id: str | None = None,
) -> None:
    """Classify a task's live trials and store one verdict."""
    from oddish.analyze import TrialClassification, compute_task_verdict

    console.print(f"[cyan]Processing task QA[/cyan] {task_id} (queue_key={queue_key})")

    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        if not task:
            raise RuntimeError(f"Task {task_id} not found in database")

        if not await _worker_job_is_running(session, worker_job_id):
            console.print(f"[dim]QA {task_id} skipped; job was cancelled[/dim]")
            return

        if task.verdict_status in (VerdictStatus.SUCCESS, VerdictStatus.FAILED):
            console.print(
                f"[yellow]Task {task_id} verdict already processed, skipping[/yellow]"
            )
            return

        task.verdict_status = VerdictStatus.RUNNING
        task.verdict_started_at = utcnow()

    verdict_result = None
    verdict_error = None
    heartbeat_stop = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    if worker_job_id:
        heartbeat_task = asyncio.create_task(
            _heartbeat_qa_worker_job(
                worker_job_id=worker_job_id,
                stop_event=heartbeat_stop,
            )
        )

    classifications: list[TrialClassification] = []
    try:
        live_trials = await _load_live_trials_for_classification(task_id)
        if not live_trials:
            # Every live trial is excluded from QA (bulk-migrated imports
            # filtered by imported_at; gate-skipped trials). Nothing to classify
            # and no inputs for a verdict -- complete the task cleanly instead of
            # raising "No successful classifications" (which would record a
            # FAILED verdict for what is not an error).
            #
            # maybe_start_qa_stage now refuses to enqueue a QA job with zero
            # eligible trials, so this is the belt-and-braces path for a race
            # (trials excluded between enqueue and run). It MUST leave a
            # TERMINAL verdict_status: QaJobHandler maps SUCCESS -> ok() and
            # everything else (including None) -> retryable failure, so a
            # non-terminal status here would burn every retry and land the job
            # FAILED. verdict stays None: dashboard counts SUCCESS AND
            # verdict->>'is_good' IN (true,false), so a null verdict is counted
            # as neither good nor bad rather than skewing either bucket.
            async with get_session() as session:
                task = await session.get(TaskModel, task_id, with_for_update=True)
                if task and await _worker_job_is_running(session, worker_job_id):
                    task.verdict = None
                    task.verdict_status = VerdictStatus.SUCCESS
                    task.verdict_error = None
                    task.verdict_finished_at = utcnow()
                    task.status = TaskStatus.COMPLETED
                    task.finished_at = utcnow()
            console.print(
                f"[yellow]QA {task_id} skipped: no QA-eligible trials "
                "(all bulk-imported)[/yellow]"
            )
            return
        to_classify = [
            trial_id
            for trial_id, analysis_status in live_trials
            if _trial_needs_classification(analysis_status)
        ]
        if to_classify:
            console.print(
                f"[cyan]Classifying {len(to_classify)} trial(s) for task "
                f"{task_id}...[/cyan]"
            )
        for trial_id in to_classify:
            async with get_session() as session:
                if not await _worker_job_is_running(session, worker_job_id):
                    console.print(
                        f"[dim]QA {task_id} classification stopped; job was cancelled[/dim]"
                    )
                    return
            try:
                await classify_trial_and_store(
                    trial_id,
                    should_store=lambda session: _worker_job_is_running(
                        session, worker_job_id
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                console.print(
                    f"[red]Classification crashed for {trial_id}: "
                    f"{type(exc).__name__}: {exc}[/red]"
                )

        async with get_session() as session:
            if not await _worker_job_is_running(session, worker_job_id):
                console.print(
                    f"[dim]QA {task_id} verdict skipped; job was cancelled[/dim]"
                )
                return
            trials_result = await session.execute(
                select(TrialModel).where(
                    TrialModel.task_id == task_id,
                    TrialModel.superseded_by_trial_id.is_(None),
                )
            )
            trials = trials_result.scalars().all()
            classifications = _classifications_from_trials(trials)

        console.print(
            f"[cyan]Computing verdict from {len(classifications)} "
            "classifications...[/cyan]"
        )
        for i, c in enumerate(classifications):
            console.print(
                f"  [{i + 1}] {c.classification.value}: {c.subtype} (reward={c.reward})"
            )

        if not classifications:
            raise ValueError("No successful classifications to synthesize verdict from")

        console.print("[dim]Starting verdict synthesis...[/dim]")
        verdict = compute_task_verdict(
            classifications=classifications,
            baseline=None,
            quality_check_passed=True,
            model=settings.verdict_model,
            console=console,
            verbose=True,
            timeout=180,
        )

        verdict_result = {
            "is_good": verdict.is_good,
            "confidence": verdict.confidence,
            "primary_issue": verdict.primary_issue,
            "reasoning": verdict.reasoning,
            "recommendations": verdict.recommendations,
            "task_problem_count": verdict.task_problem_count,
            "agent_problem_count": verdict.agent_problem_count,
            "success_count": verdict.success_count,
            "harness_error_count": verdict.harness_error_count,
        }

        console.print(
            f"[green]Verdict computed:[/green] {'GOOD' if verdict.is_good else 'NEEDS REVIEW'} "
            f"(confidence: {verdict.confidence})"
        )

    except asyncio.CancelledError:
        verdict_error = (
            "Verdict synthesis was cancelled by the worker runtime before it finished. "
            "This is usually caused by a worker restart or shutdown."
        )
        console.print(f"[yellow]Verdict cancelled for {task_id}[/yellow]")
    except Exception as e:
        verdict_error = f"{type(e).__name__}: {e}"
        console.print(f"[red]Verdict error for {task_id}: {verdict_error}[/red]")
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _store_results() -> None:
        async with get_session() as session:
            task = await session.get(TaskModel, task_id, with_for_update=True)
            if not task:
                return

            if not await _worker_job_is_running(session, worker_job_id):
                console.print(
                    f"[dim]QA {task_id} result ignored; job was cancelled[/dim]"
                )
                return

            if verdict_result:
                task.verdict = verdict_result
                task.verdict_status = VerdictStatus.SUCCESS
                task.verdict_error = None
                task.verdict_finished_at = utcnow()
                task.status = TaskStatus.COMPLETED
                task.finished_at = utcnow()
                console.print(
                    f"[green]Verdict {task_id} SUCCESS - Task COMPLETED[/green]"
                )
            else:
                task.verdict_status = VerdictStatus.FAILED
                task.verdict_error = (
                    verdict_error or "Verdict synthesis failed with exception"
                )
                task.verdict_finished_at = utcnow()
                task.status = TaskStatus.COMPLETED
                task.finished_at = utcnow()
                console.print(
                    f"[yellow]Verdict {task_id} FAILED - Task COMPLETED (no verdict)[/yellow]"
                )

    await asyncio.shield(_store_results())

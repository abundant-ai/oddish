from __future__ import annotations

import asyncio

from sqlalchemy import select

from oddish.config import settings
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TrialModel,
    VerdictStatus,
    get_session,
    utcnow,
)
from oddish.workers.queue.analysis_handler import classify_trial_and_store
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

VERDICT_HEARTBEAT_INTERVAL_SECONDS = 30


async def _heartbeat_verdict_worker_job(
    *,
    worker_job_id: str,
    stop_event: asyncio.Event,
) -> None:
    """Keep ``worker_jobs.heartbeat_at`` fresh during verdict synthesis."""
    consecutive_failures = 0
    pending_failure_count = 0
    pending_last_error: str | None = None

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=VERDICT_HEARTBEAT_INTERVAL_SECONDS
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
                    f"[green]Verdict worker_job {worker_job_id} heartbeat "
                    f"recovered after {consecutive_failures} failure(s)[/green]"
                )
            consecutive_failures = 0
            pending_failure_count = 0
            pending_last_error = None
        except Exception as exc:
            consecutive_failures += 1
            pending_failure_count += 1
            pending_last_error = f"{type(exc).__name__}: {exc}"


def _trial_needs_classification(analysis_status: AnalysisStatus | None) -> bool:
    """Whether the task QA job should (re)classify this live trial.

    Trials whose analysis is already terminal (SUCCESS / FAILED) keep
    their stored result; everything else (NULL / PENDING / QUEUED /
    RUNNING) gets (re)classified. Probe trials are normally already
    SUCCESS (analyzed inline by the trial job) so they are reused here;
    if a probe trial still needs analysis, ``classify_trial_and_store``
    routes it through the probe analyzer.
    """
    return analysis_status not in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED)


def _classifications_from_trials(trials) -> list:
    """Reconstruct ``TrialClassification`` objects from stored analyses.

    Only successfully-classified trials contribute, and probe summaries
    (which store no ``classification`` key) are skipped defensively so a
    task mixing probe and classified trials can't crash verdict synthesis.
    """
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
    """Return ``(trial_id, analysis_status)`` for live trials.

    "Live" means not superseded by a retry. Used to decide which trials
    the task-level QA job still needs to classify.
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
                )
            )
        ).all()

    return [(str(trial_id), analysis_status) for trial_id, analysis_status in rows]


async def run_verdict_job(
    task_id: str,
    queue_key: str,
    modal_function_call_id: str | None = None,
    worker_job_id: str | None = None,
) -> None:
    """Run task-level trajectory analysis (QA) for a claimed task.

    This is the single, task-scoped QA job that replaced the old
    per-trial ``ANALYSIS`` jobs plus the separate per-task ``VERDICT``
    job. One worker job per task now:

    1. Classifies every live (non-superseded) trial's trajectory with the
       Claude Code classifier -- same taxonomy, evidence, and reasoning as
       before -- writing each result to ``trial.analysis`` /
       ``trial.analysis_status``. Trials already classified (SUCCESS) or
       analyzed inline (probe trials) are reused, not re-run.
    2. Synthesizes a single task verdict from those per-trial
       classifications and stores it on ``task.verdict``.
    3. Marks the task COMPLETED.

    Collapsing N analyses + 1 verdict into one job means a sweep of
    ``T`` tasks x ``N`` trials enqueues ``T`` QA jobs instead of
    ``T * (N + 1)``.
    """
    from oddish.analyze import TrialClassification, compute_task_verdict

    console.print(
        f"[cyan]Processing task QA[/cyan] {task_id} (queue_key={queue_key})"
    )

    # Mark as running. The per-trial classification + verdict synthesis
    # below can be long (it walks every trial), so a single heartbeat
    # loop spans the whole job.
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        if not task:
            raise RuntimeError(f"Task {task_id} not found in database")

        # Skip if already processed
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
            _heartbeat_verdict_worker_job(
                worker_job_id=worker_job_id,
                stop_event=heartbeat_stop,
            )
        )

    classifications: list[TrialClassification] = []
    try:
        # -----------------------------------------------------------------
        # 1. Classification phase: classify every live trial that still
        #    needs it. Trials already SUCCESS/FAILED keep their stored
        #    result (probe trials, analyzed inline by the trial job, are
        #    normally already SUCCESS and thus reused). Each classification
        #    is best-effort -- one failed trial must not abort the whole
        #    task QA -- and ``classify_trial_and_store`` records FAILED on
        #    error itself.
        # -----------------------------------------------------------------
        live_trials = await _load_live_trials_for_classification(task_id)
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
            try:
                await classify_trial_and_store(trial_id)
            except Exception as exc:  # noqa: BLE001
                console.print(
                    f"[red]Classification crashed for {trial_id}: "
                    f"{type(exc).__name__}: {exc}[/red]"
                )

        # -----------------------------------------------------------------
        # 2. Verdict phase: synthesize the task verdict from the live
        #    trials' classifications. Filter out superseded rows so the
        #    verdict only reflects the currently-live trial set.
        # -----------------------------------------------------------------
        async with get_session() as session:
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
                f"  [{i + 1}] {c.classification.value}: {c.subtype} "
                f"(reward={c.reward})"
            )

        if not classifications:
            raise ValueError("No successful classifications to synthesize verdict from")

        console.print("[dim]Starting verdict synthesis...[/dim]")
        verdict = compute_task_verdict(
            classifications=classifications,
            baseline=None,  # We don't have baseline validation data
            quality_check_passed=True,  # Assume passed
            model=settings.verdict_model,
            console=console,
            verbose=True,
            timeout=180,  # 3 minutes
        )

        # Convert to dict for storage
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
            task = await session.get(TaskModel, task_id)
            if not task:
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
                # Still mark task as completed even if verdict failed
                task.status = TaskStatus.COMPLETED
                task.finished_at = utcnow()
                console.print(
                    f"[yellow]Verdict {task_id} FAILED - Task COMPLETED (no verdict)[/yellow]"
                )

    await asyncio.shield(_store_results())

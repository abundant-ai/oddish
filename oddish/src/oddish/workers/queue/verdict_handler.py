from __future__ import annotations

import asyncio

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
from oddish.workers.queue.shared import console


async def run_verdict_job(task_id: str, queue_key: str) -> None:
    """
    Execute verdict synthesis for a claimed task.

    1. Load all trial classifications from database
    2. Run verdict synthesis with Claude
    3. Store verdict in task.verdict
    4. Mark task as COMPLETED
    """
    from oddish.analyze import (
        Classification,
        TrialClassification,
        compute_task_verdict,
    )

    console.print(
        f"[cyan]Processing verdict[/cyan] {task_id} (queue_key={queue_key})"
    )

    # Mark as running and load classifications
    classifications = []
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

        # Load trial classifications
        from sqlalchemy import select

        trials_result = await session.execute(
            select(TrialModel).where(TrialModel.task_id == task_id)
        )
        trials = trials_result.scalars().all()

        for trial in trials:
            if trial.analysis and trial.analysis_status == AnalysisStatus.SUCCESS:
                # Reconstruct TrialClassification from stored dict
                analysis = trial.analysis
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

        await session.commit()

    console.print(
        f"[cyan]Computing verdict from {len(classifications)} classifications...[/cyan]"
    )
    for i, c in enumerate(classifications):
        console.print(
            f"  [{i + 1}] {c.classification.value}: {c.subtype} (reward={c.reward})"
        )

    # Run verdict synthesis
    verdict_result = None
    verdict_error = None

    try:
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

from __future__ import annotations

import json

from pgqueuer.models import Job

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


async def run_verdict_job(job: Job, provider: str) -> None:
    """
    Handle a verdict job from PGQueuer.

    This synthesizes trial classifications into a task verdict:
    1. Load all trial classifications from database
    2. Run verdict synthesis with Claude
    3. Store verdict in task.verdict
    4. Mark task as COMPLETED
    """
    from swegen.analyze import Classification, TrialClassification, compute_task_verdict

    payload = json.loads(job.payload.decode())
    task_id = payload.get("task_id")

    if not task_id:
        raise ValueError(f"Invalid verdict job payload (missing task_id): {payload}")

    console.print(
        f"[cyan]Processing verdict[/cyan] {task_id} (provider={provider}, pgqueuer_job_id={job.id})"
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
            f"  [{i+1}] {c.classification.value}: {c.subtype} (reward={c.reward})"
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
            model="gpt-5.2",
            console=console,
            verbose=True,
            timeout=180,  # 3 minutes
        )

        # Convert to dict for storage
        verdict_result = {
            "is_good": verdict.is_good,
            "confidence": verdict.confidence,
            "primary_issue": verdict.primary_issue,
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

    except Exception as e:
        verdict_error = f"{type(e).__name__}: {e}"
        console.print(f"[red]Verdict error for {task_id}: {verdict_error}[/red]")

    # Store results
    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        if not task:
            return

        if verdict_result:
            task.verdict = verdict_result
            task.verdict_status = VerdictStatus.SUCCESS
            task.verdict_finished_at = utcnow()
            task.status = TaskStatus.COMPLETED
            task.finished_at = utcnow()
            console.print(f"[green]Verdict {task_id} SUCCESS - Task COMPLETED[/green]")
        else:
            task.verdict_status = VerdictStatus.FAILED
            task.verdict_error = verdict_error
            task.verdict_finished_at = utcnow()
            # Still mark task as completed even if verdict failed
            task.status = TaskStatus.COMPLETED
            task.finished_at = utcnow()
            console.print(
                f"[yellow]Verdict {task_id} FAILED - Task COMPLETED (no verdict)[/yellow]"
            )

        await session.commit()

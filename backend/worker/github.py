from .runtime import console


async def notify_github_trial(trial_id: str) -> None:
    """Notify GitHub of trial completion. Analysis trials (QA, audit,
    analyzer) never post trial status; the QA import hook owns that comment."""
    try:
        from sqlalchemy import select

        from oddish.db import TrialModel, get_session
        from oddish.integrations.github import notify_trial_update
        from oddish.workers.analysis_trials import is_analysis_kind

        async with get_session() as session:
            kind = await session.scalar(
                select(TrialModel.kind).where(TrialModel.id == trial_id)
            )
        if is_analysis_kind(kind):
            return
        await notify_trial_update(trial_id)
    except Exception as e:
        console.print(f"[yellow]GitHub notification failed (trial): {e}[/yellow]")


async def notify_github_qa(task_id: str) -> None:
    """Notify GitHub when a task's QA job completes.

    Refreshes the whole PR comment in one update (every trial's
    classification plus the task verdict).
    """
    try:
        from oddish.integrations.github import notify_qa_update

        await notify_qa_update(task_id)
    except Exception as e:
        console.print(f"[yellow]GitHub notification failed (qa): {e}[/yellow]")

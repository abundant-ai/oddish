from .runtime import console


async def notify_github_trial(trial_id: str) -> None:
    """Notify GitHub of trial completion. Analysis trials never post trial
    status (the notifier skips them); the QA import hook owns that comment."""
    try:
        from oddish.integrations.github import notify_trial_update

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

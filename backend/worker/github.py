from .runtime import console


async def notify_github_trial(trial_id: str) -> None:
    """Notify GitHub of trial completion."""
    try:
        from oddish.integrations.github import notify_trial_update

        await notify_trial_update(trial_id)
    except Exception as e:
        console.print(f"[yellow]GitHub notification failed (trial): {e}[/yellow]")


async def notify_github_analysis(trial_id: str) -> None:
    """Notify GitHub of analysis completion."""
    try:
        from oddish.integrations.github import notify_analysis_update

        await notify_analysis_update(trial_id)
    except Exception as e:
        console.print(f"[yellow]GitHub notification failed (analysis): {e}[/yellow]")


async def notify_github_verdict(task_id: str) -> None:
    """Notify GitHub of verdict completion."""
    try:
        from oddish.integrations.github import notify_verdict_update

        await notify_verdict_update(task_id)
    except Exception as e:
        console.print(f"[yellow]GitHub notification failed (verdict): {e}[/yellow]")

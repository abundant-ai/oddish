"""Re-export from oddish — GitHub integration now lives in the core package."""

from oddish.integrations.github import (  # noqa: F401
    notify_trial_update,
    notify_analysis_update,
    notify_verdict_update,
)

__all__ = [
    "notify_trial_update",
    "notify_analysis_update",
    "notify_verdict_update",
]

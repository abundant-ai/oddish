"""
GitHub integration for Oddish.

Updates PR comments with trial/analysis/verdict progress.

Requires GITHUB_TOKEN environment variable for API access.
"""

from __future__ import annotations

from .dispatch import maybe_dispatch_experiment_complete, maybe_dispatch_for_task
from .notifier import notify_trial_update, notify_analysis_update, notify_verdict_update

__all__ = [
    "notify_trial_update",
    "notify_analysis_update",
    "notify_verdict_update",
    "maybe_dispatch_experiment_complete",
    "maybe_dispatch_for_task",
]

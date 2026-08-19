"""
GitHub integration for Oddish.

Updates PR comments as trials run and as the task-level QA job completes.

Requires GITHUB_TOKEN environment variable for API access.
"""

from __future__ import annotations

from .notifier import (
    notify_qa_update,
    notify_trial_update,
)

__all__ = [
    "notify_trial_update",
    "notify_qa_update",
]

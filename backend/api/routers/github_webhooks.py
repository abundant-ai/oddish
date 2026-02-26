"""
GitHub webhook endpoints for manual PR comment updates.

This router provides endpoints to manually trigger PR comment updates,
useful for testing and debugging the GitHub integration.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import AuthContext, require_auth, APIKeyScope
from oddish.db import TaskModel, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/github", tags=["GitHub"])


class RefreshResponse(BaseModel):
    """Response from refresh endpoint."""

    success: bool
    message: str
    pr_url: str | None = None


@router.post("/tasks/{task_id}/refresh", response_model=RefreshResponse)
async def refresh_task_pr_comment(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> RefreshResponse:
    """
    Manually refresh the PR comment for a task.

    Useful for testing the GitHub integration or forcing an update.
    """
    auth.require_scope(APIKeyScope.TASKS)

    async with get_session() as session:
        task = await session.get(TaskModel, task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        # Check org access
        if task.org_id != auth.org_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Check for GitHub metadata
        from integrations.github.client import GitHubMeta

        github_meta = GitHubMeta.from_tags(task.tags)
        if not github_meta:
            raise HTTPException(
                status_code=400,
                detail="Task does not have GitHub metadata (not from a PR)",
            )

        # Trigger update
        from integrations.github.notifier import _update_pr_comment_for_task

        success = await _update_pr_comment_for_task(task)

        if success:
            return RefreshResponse(
                success=True,
                message=f"Updated PR comment for {github_meta.owner}/{github_meta.repo}#{github_meta.pr_number}",
                pr_url=github_meta.pr_url,
            )
        else:
            return RefreshResponse(
                success=False,
                message="Failed to update PR comment. Check logs for details.",
                pr_url=github_meta.pr_url,
            )


@router.post("/experiments/{experiment_id}/refresh", response_model=RefreshResponse)
async def refresh_experiment_pr_comment(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> RefreshResponse:
    """
    Manually refresh the PR comment for all tasks in an experiment.

    Useful for testing the GitHub integration or forcing an update.
    """
    auth.require_scope(APIKeyScope.TASKS)

    from sqlalchemy import select

    async with get_session() as session:
        # Get first task to check for GitHub metadata
        result = await session.execute(
            select(TaskModel)
            .where(TaskModel.experiment_id == experiment_id)
            .where(TaskModel.org_id == auth.org_id)
            .limit(1)
        )
        task = result.scalar_one_or_none()

        if not task:
            raise HTTPException(
                status_code=404,
                detail=f"No tasks found in experiment {experiment_id}",
            )

        # Check for GitHub metadata
        from integrations.github.client import GitHubMeta

        github_meta = GitHubMeta.from_tags(task.tags)
        if not github_meta:
            raise HTTPException(
                status_code=400,
                detail="Experiment tasks do not have GitHub metadata (not from a PR)",
            )

        # Trigger update (will aggregate all tasks)
        from integrations.github.notifier import _update_pr_comment_for_task

        success = await _update_pr_comment_for_task(task)

        if success:
            return RefreshResponse(
                success=True,
                message=f"Updated PR comment for {github_meta.owner}/{github_meta.repo}#{github_meta.pr_number}",
                pr_url=github_meta.pr_url,
            )
        else:
            return RefreshResponse(
                success=False,
                message="Failed to update PR comment. Check logs for details.",
                pr_url=github_meta.pr_url,
            )


@router.get("/status")
async def github_integration_status(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """
    Check the status of the GitHub integration.

    Returns whether the GITHUB_TOKEN is configured.
    """
    auth.require_scope(APIKeyScope.READ)

    token_configured = bool(os.getenv("GITHUB_TOKEN"))

    return {
        "token_configured": token_configured,
    }

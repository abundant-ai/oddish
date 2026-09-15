from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import Response
from harbor.models.environment_type import EnvironmentType

from api.routers import tasks
from oddish.core.endpoints import SweepAttribution
from oddish.schemas import (
    AgentModelPair,
    TaskSweepBatchItemResult,
    TaskSweepBatchRequest,
    TaskSweepSubmission,
)


@pytest.mark.asyncio
async def test_batch_hashes_each_raw_item_before_identity_defaults(
    monkeypatch,
) -> None:
    hashes = {"task-a": "0" * 64, "task-b": "f" * 64}
    hashed_users = []
    routed = []

    def compute_request_hash(submission):
        hashed_users.append(submission.user)
        return hashes[submission.task_id]

    async def resolve_submission_identity(_session, submission, _auth):
        submission.user = "server-default"

    def get_default_cloud_environment(submission, *, request_hash):
        routed.append((submission.task_id, submission.user, request_hash))
        return EnvironmentType.ARCHIL

    async def require_connected_github_user(*_args):
        return None

    async def resolve_sweep_attribution(*_args):
        return SweepAttribution()

    async def create_batch_core(session, *, submissions, prepare, **_kwargs):
        results = []
        for index, submission in enumerate(submissions):
            await prepare(session, submission)
            results.append(
                TaskSweepBatchItemResult(
                    index=index,
                    success=False,
                    status_code=400,
                    error="test stop after prepare",
                )
            )
        return results

    class Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def get_session():
        yield Session()

    async def spawn_gke_image_builds(*_args):
        pass

    monkeypatch.setattr(tasks, "compute_request_hash", compute_request_hash)
    monkeypatch.setattr(
        tasks, "resolve_submission_identity", resolve_submission_identity
    )
    monkeypatch.setattr(tasks, "apply_github_attribution", lambda _submission: None)
    monkeypatch.setattr(
        tasks, "require_connected_github_user", require_connected_github_user
    )
    monkeypatch.setattr(tasks, "resolve_sweep_attribution", resolve_sweep_attribution)
    monkeypatch.setattr(
        tasks, "get_default_cloud_environment", get_default_cloud_environment
    )
    monkeypatch.setattr(tasks, "create_task_sweep_batch_core", create_batch_core)
    monkeypatch.setattr(tasks, "get_session", get_session)
    monkeypatch.setattr(tasks, "_spawn_gke_image_builds", spawn_gke_image_builds)

    payload = TaskSweepBatchRequest(
        submissions=[
            TaskSweepSubmission(
                task_id=task_id,
                configs=[AgentModelPair(agent="oracle")],
            )
            for task_id in hashes
        ]
    )
    auth = SimpleNamespace(
        org_id="org-1",
        user_id="user-1",
        require_scope=lambda _scope: None,
    )

    result = await tasks.create_task_sweep_batch(payload, auth, Response())

    assert result.failed == 2
    assert hashed_users == [None, None]
    assert routed == [
        ("task-a", "server-default", hashes["task-a"]),
        ("task-b", "server-default", hashes["task-b"]),
    ]

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.routers import tasks
from oddish.schemas import AgentModelPair, TaskResponse, TaskSweepSubmission


@pytest.mark.asyncio
async def test_add_trials_replays_even_if_original_runs_have_failed(monkeypatch):
    saved = TaskResponse(
        id="task",
        name="task",
        status="running",
        priority="low",
        trials_count=5,
        providers={"bedrock": 5},
        created_at=datetime.now(UTC),
        new_trial_ids=["trial"],
    )

    @asynccontextmanager
    async def session():
        yield object()

    probe_failed = AsyncMock(return_value=True)
    create = AsyncMock()
    monkeypatch.setattr(tasks, "get_session", session)
    monkeypatch.setattr(
        tasks,
        "probe_completed_replay",
        AsyncMock(return_value=saved.model_dump(mode="json")),
    )
    monkeypatch.setattr(tasks, "replay_has_retryable_failed_trials", probe_failed)
    monkeypatch.setattr(tasks, "create_task_sweep_core", create)
    response = await tasks.create_task_sweep(
        TaskSweepSubmission(
            task_id="task",
            append_to_task=True,
            add_trials=True,
            configs=[
                AgentModelPair(
                    agent="claude-code",
                    model="global.anthropic.claude-opus-5",
                    n_trials=5,
                    agent_config={"kwargs": {"reasoning_effort": "high"}},
                )
            ],
        ),
        SimpleNamespace(org_id="org", require_scope=lambda scope: None),
        idempotency_key="operation:task",
    )
    assert response == saved
    probe_failed.assert_not_awaited()
    create.assert_not_awaited()

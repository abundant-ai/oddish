"""Model-normalization errors stay client errors on fresh and top-up sweeps."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.endpoints.sweep import create_task_sweep_core
from oddish.core.sweeps import build_trial_specs_from_sweep
from oddish.schemas import AgentModelPair, TaskSweepSubmission


def submission():
    return TaskSweepSubmission(
        task_id="unmapped-claude-task",
        configs=[
            AgentModelPair(agent="claude-code", model="anthropic/claude-unmapped-test")
        ],
    )


@pytest.mark.parametrize("existing_counts", [None, {}], ids=["create", "top-up"])
def test_unmapped_model_is_a_client_error(existing_counts):
    with pytest.raises(HTTPException) as caught:
        build_trial_specs_from_sweep(submission(), existing_counts=existing_counts)
    assert caught.value.status_code == 404
    assert "No Bedrock model id mapping" in caught.value.detail
    assert "anthropic/claude-unmapped-test" in caught.value.detail


@pytest.mark.asyncio
async def test_create_sweep_returns_404_before_admission_or_creation(monkeypatch):
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = None
    create = AsyncMock()
    admit = AsyncMock()
    monkeypatch.setattr(
        "oddish.core.tasks.resolve_task_storage",
        AsyncMock(return_value=("test-task", None)),
    )
    monkeypatch.setattr("oddish.queue.create_task", create)
    monkeypatch.setattr("oddish.core.quota_admission.admit_trials", admit)
    app = FastAPI()

    @app.post("/tasks/sweep")
    async def submit(payload: TaskSweepSubmission):
        return await create_task_sweep_core(session, submission=payload)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/tasks/sweep", json=submission().model_dump(mode="json")
        )
    assert response.status_code == 404
    assert "No Bedrock model id mapping" in response.json()["detail"]
    create.assert_not_awaited()
    admit.assert_not_awaited()

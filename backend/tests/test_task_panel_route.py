from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.routers import tasks
from models import APIKeyScope


@pytest.mark.asyncio
async def test_panel_uses_read_session_and_verified_org(monkeypatch):
    session = object()

    @asynccontextmanager
    async def read_session():
        yield session

    reader = AsyncMock(return_value=object())
    monkeypatch.setattr(tasks, "get_read_session", read_session)
    monkeypatch.setattr(tasks, "get_task_panel_core", reader)
    auth = SimpleNamespace(org_id="verified-org", require_scope=Mock())
    await tasks.get_task_panel("task-1", auth, version=2)
    auth.require_scope.assert_called_once_with(APIKeyScope.READ)
    reader.assert_awaited_once_with(
        session, task_id="task-1", version=2, org_id="verified-org"
    )
    assert (
        sum(
            getattr(route, "path", None) == "/tasks/{task_id}/panel"
            for route in tasks.router.routes
        )
        == 1
    )

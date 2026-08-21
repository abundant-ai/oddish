from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from api.routers import tasks
from api.schemas import ModelRenameRequest
from auth import AuthContext, AuthMethod
from models import UserRole


@pytest.mark.asyncio
async def test_model_rename_locks_experiment_before_merging_map(monkeypatch):
    experiment = SimpleNamespace(
        name="public benchmark",
        public_model_renames={"openai/gpt-5": "GPT"},
    )

    class Result:
        def scalar_one_or_none(self):
            return experiment

    class Session:
        statement = None
        committed = False

        async def execute(self, statement):
            self.statement = statement
            return Result()

        async def commit(self):
            self.committed = True

    session = Session()

    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(tasks, "get_session", get_session)
    auth = AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_1",
        user_id="admin_1",
        user_role=UserRole.ADMIN,
    )

    response = await tasks.set_experiment_model_rename(
        "exp_1",
        ModelRenameRequest(model="anthropic/claude-sonnet", display="Sonnet"),
        auth,
    )

    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    assert session.committed
    assert response.renames == {
        "openai/gpt-5": "GPT",
        "anthropic/claude-sonnet": "Sonnet",
    }

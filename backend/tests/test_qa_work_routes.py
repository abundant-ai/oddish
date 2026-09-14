from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from auth import AuthContext, require_admin
from auth.types import AuthMethod
from fastapi import HTTPException
from models import APIKeyScope

from api.routers import qa_work
from oddish.schemas import QAWorkAssign, QAWorkAssignResponse


def test_assignment_route_requires_admin():
    route = next(route for route in qa_work.router.routes if "POST" in route.methods)
    assert require_admin in [
        dependency.call for dependency in route.dependant.dependencies
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [APIKeyScope.TASKS, APIKeyScope.READ])
async def test_non_admin_keys_cannot_assign(scope):
    auth = AuthContext(method=AuthMethod.API_KEY, org_id="org1", scope=scope)
    with pytest.raises(HTTPException) as exc:
        await require_admin(auth)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("assignee", ["alice@example.com", "@alice", "user-alice"])
async def test_assignment_resolves_org_member_and_passes_canonical_id(
    monkeypatch, assignee
):
    session = MagicMock()
    session.scalars = AsyncMock(
        return_value=SimpleNamespace(all=lambda: [SimpleNamespace(id="user-alice")])
    )
    session.commit = AsyncMock()

    @asynccontextmanager
    async def get_session():
        yield session

    assign = AsyncMock(
        return_value=QAWorkAssignResponse(
            owner_user_id="user-alice", assigned_task_ids=["task1"]
        )
    )
    monkeypatch.setattr(qa_work, "get_session", get_session)
    monkeypatch.setattr(qa_work, "assign_task_qa_work_core", assign)
    auth = AuthContext(method=AuthMethod.API_KEY, org_id="org1", scope=APIKeyScope.FULL)
    await qa_work.assign_qa_work(
        QAWorkAssign(task_ids=["task1"], assignee=assignee, replace=True), auth
    )
    statement = session.scalars.call_args.args[0]
    assert statement.compile().params["org_id_1"] == "org1"
    assign.assert_awaited_once_with(
        session,
        org_id="org1",
        task_ids=["task1"],
        owner_user_id="user-alice",
        replace=True,
    )
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("users, code", [([], 404), (["user1", "user2"], 409)])
async def test_unknown_or_ambiguous_assignee_does_not_mutate(monkeypatch, users, code):
    session = MagicMock()
    session.scalars = AsyncMock(
        return_value=SimpleNamespace(
            all=lambda: [SimpleNamespace(id=user_id) for user_id in users]
        )
    )
    session.commit = AsyncMock()

    @asynccontextmanager
    async def get_session():
        yield session

    assign = AsyncMock()
    monkeypatch.setattr(qa_work, "get_session", get_session)
    monkeypatch.setattr(qa_work, "assign_task_qa_work_core", assign)
    auth = AuthContext(method=AuthMethod.API_KEY, org_id="org1", scope=APIKeyScope.FULL)
    with pytest.raises(HTTPException) as exc:
        await qa_work.assign_qa_work(
            QAWorkAssign(task_ids=["task1"], assignee="alice"), auth
        )
    assert exc.value.status_code == code
    assign.assert_not_awaited()
    session.commit.assert_not_awaited()

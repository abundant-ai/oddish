from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from api.routers import dashboard as dashboard_router
from auth import AuthContext, AuthMethod, require_auth
from oddish.core.admin import CostLeaderboardUser


class _ScalarResult:
    def __init__(self, users):
        self._users = users

    def scalars(self):
        return self._users


class _Session:
    def __init__(self, users):
        self._users = users

    async def execute(self, _query):
        return _ScalarResult(self._users)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router.router)
    return app


@pytest.mark.asyncio
async def test_leaderboard_requires_auth() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/leaderboard")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_leaderboard_returns_only_name_and_cost(monkeypatch) -> None:
    users = [
        SimpleNamespace(id="user-1", name="Ada", github_username="ada"),
        SimpleNamespace(id="user-2", name=None, github_username="grace"),
        SimpleNamespace(
            id="user-3",
            name=None,
            github_username=None,
            email="private@example.com",
        ),
    ]

    @asynccontextmanager
    async def fake_get_session():
        yield _Session(users)

    seen: dict[str, str | int | None] = {}

    async def fake_leaderboard(_session, *, org_id, window_days):
        seen["org_id"] = org_id
        seen["window_days"] = window_days
        return [
            CostLeaderboardUser(user_id="user-1", cost_usd=12.34),
            CostLeaderboardUser(user_id="user-2", cost_usd=8.5),
            CostLeaderboardUser(user_id="user-3", cost_usd=7.0),
        ]

    monkeypatch.setattr(dashboard_router, "get_session", fake_get_session)
    monkeypatch.setattr(dashboard_router, "get_cost_leaderboard_core", fake_leaderboard)
    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.API_KEY, org_id="org-1"
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/leaderboard?window_days=0&limit=5")

    assert response.status_code == 200
    assert seen == {"org_id": "org-1", "window_days": None}
    assert response.json() == {
        "leaders": [
            {"name": "Ada", "cost_usd": 12.34},
            {"name": "@grace", "cost_usd": 8.5},
        ]
    }
    assert set(response.json()["leaders"][0]) == {"name", "cost_usd"}

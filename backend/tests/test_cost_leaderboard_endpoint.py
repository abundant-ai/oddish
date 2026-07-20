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

    def all(self):
        return [
            (user.id, user.name, user.github_username, user.email)
            for user in self._users
        ]


class _Session:
    def __init__(self, users):
        self._users = users
        self.execute_calls = 0

    async def execute(self, _query):
        self.execute_calls += 1
        return _ScalarResult(self._users)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router.router)
    return app


def _client_app(monkeypatch, users, ranked) -> tuple[FastAPI, _Session]:
    session = _Session(users)

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_leaderboard(_session, *, org_id, window_days, resolve_github_users):
        return ranked

    monkeypatch.setattr(dashboard_router, "get_session", fake_get_session)
    monkeypatch.setattr(dashboard_router, "get_cost_leaderboard_core", fake_leaderboard)
    app = _app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.API_KEY, org_id="org-1"
    )
    return app, session


@pytest.mark.asyncio
async def test_leaderboard_requires_auth() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/leaderboard")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_leaderboard_returns_only_rank_name_and_cost(monkeypatch) -> None:
    users = [
        SimpleNamespace(
            id="user-1", name="Ada", github_username="ada", email="ada@example.com"
        ),
        SimpleNamespace(
            id="user-2", name=None, github_username="grace", email="g@example.com"
        ),
        SimpleNamespace(
            id="user-3",
            name=None,
            github_username=None,
            email="private@example.com",
        ),
    ]

    seen: dict[str, str | int | None] = {}

    session = _Session(users)

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def fake_leaderboard(_session, *, org_id, window_days, resolve_github_users):
        seen["org_id"] = org_id
        seen["window_days"] = window_days
        # Spend tagged with a handle belongs to whoever owns it, so the endpoint
        # has to hand the resolver down or the ranking under-counts that person.
        seen["resolver"] = resolve_github_users.__name__
        return [
            CostLeaderboardUser(user_id="user-3", cost_usd=20.0),
            CostLeaderboardUser(user_id="user-1", cost_usd=12.34),
            CostLeaderboardUser(user_id="user-2", cost_usd=8.5),
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
        response = await client.get("/leaderboard?window_days=0&limit=3")

    assert response.status_code == 200
    assert seen == {
        "org_id": "org-1",
        "window_days": None,
        "resolver": "resolve_github_users",
    }
    # An email-only user shows their email local part, never the full address.
    assert response.json() == {
        "leaders": [
            {"rank": 1, "name": "private", "cost_usd": 20.0},
            {"rank": 2, "name": "Ada", "cost_usd": 12.34},
            {"rank": 3, "name": "@grace", "cost_usd": 8.5},
        ]
    }
    assert set(response.json()["leaders"][0]) == {"rank", "name", "cost_usd"}
    assert "private@example.com" not in response.text
    assert session.execute_calls == 1


@pytest.mark.asyncio
async def test_leaderboard_keeps_github_buckets_and_preserves_rank(
    monkeypatch,
) -> None:
    users = [
        SimpleNamespace(
            id="user-1", name=None, github_username=None, email="kate@example.com"
        ),
        SimpleNamespace(
            id="user-2", name="Ada", github_username="ada", email="ada@example.com"
        ),
    ]
    ranked = [
        # GitHub-identity bucket with no registered user: label passes through.
        CostLeaderboardUser(label="@ghost", cost_usd=30.0),
        CostLeaderboardUser(user_id="user-1", cost_usd=20.0),
        # Unresolvable payer (e.g. outside the auth org): dropped, rank kept.
        CostLeaderboardUser(user_id="user-gone", cost_usd=10.0),
        CostLeaderboardUser(user_id="user-2", cost_usd=5.0),
    ]
    app, _session = _client_app(monkeypatch, users, ranked)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/leaderboard?window_days=7&limit=100")

    assert response.status_code == 200
    assert response.json() == {
        "leaders": [
            {"rank": 1, "name": "@ghost", "cost_usd": 30.0},
            {"rank": 2, "name": "kate", "cost_usd": 20.0},
            {"rank": 4, "name": "Ada", "cost_usd": 5.0},
        ]
    }

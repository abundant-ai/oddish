import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import prompts as prompts_router
from auth import APIKeyScope, AuthContext, AuthMethod, require_auth


class _FakeVersion:
    def __init__(self, version, content):
        self.version = version
        self.content = content
        self.created_at = __import__("datetime").datetime.now()
        self.created_by = None


class _FakePrompt:
    id = "p1"
    key = "pre_trial_qa"
    description = "d"
    active_version = 1
    created_at = __import__("datetime").datetime.now()
    updated_at = __import__("datetime").datetime.now()


@contextlib.asynccontextmanager
async def _ctx(_):
    yield None


def _auth(scopes):
    # AuthContext carries a single `scope` (hierarchy FULL>TASKS>READ),
    # not a list; take the first requested scope.
    scope = list(scopes)[0]

    def _factory():
        return AuthContext(
            method=AuthMethod.API_KEY, org_id="org_1", user_id="u1", scope=scope
        )
    return _factory


async def _call(method, path, monkeypatch, *, scopes=(APIKeyScope.READ,), **kwargs):
    async def fake_get(session, key, *, version=None):
        return _FakePrompt(), _FakeVersion(1, "hello")

    monkeypatch.setattr(prompts_router, "get_session", lambda: _ctx(None))
    monkeypatch.setattr(prompts_router, "get_prompt_core", fake_get)
    app = create_app()
    app.dependency_overrides[require_auth] = _auth(list(scopes))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_get_prompt_returns_active_content(monkeypatch):
    resp = await _call("GET", "/prompts/pre_trial_qa", monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "pre_trial_qa"
    assert body["content"] == "hello"


@pytest.mark.asyncio
async def test_put_requires_write_scope(monkeypatch):
    # READ-only scope must be rejected on write
    resp = await _call(
        "PUT", "/prompts/pre_trial_qa", monkeypatch,
        scopes=(APIKeyScope.READ,), json={"content": "x"},
    )
    assert resp.status_code in (401, 403)

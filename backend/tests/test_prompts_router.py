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
    kind = "QA_PRE_TRIAL"
    description = "d"
    created_at = __import__("datetime").datetime.now()
    updated_at = __import__("datetime").datetime.now()


class _FakeSession:
    async def commit(self):
        pass


@contextlib.asynccontextmanager
async def _ctx(_):
    yield _FakeSession()


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
    async def fake_get(session, kind, *, version=None):
        return _FakePrompt(), _FakeVersion(1, "hello")

    monkeypatch.setattr(prompts_router, "get_session", lambda: _ctx(None))
    monkeypatch.setattr(prompts_router, "get_prompt_core", fake_get)
    app = create_app()
    app.dependency_overrides[require_auth] = _auth(list(scopes))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_get_prompt_returns_latest_content(monkeypatch):
    resp = await _call("GET", "/prompts/QA_PRE_TRIAL", monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "QA_PRE_TRIAL"
    assert body["latest_version"] == 1
    assert body["content"] == "hello"


@pytest.mark.asyncio
async def test_get_unknown_kind_is_422(monkeypatch):
    # kind path params are PromptKind-typed; arbitrary strings never reach core
    resp = await _call("GET", "/prompts/not_a_kind", monkeypatch)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_requires_write_scope(monkeypatch):
    # READ-only scope must be rejected on write
    resp = await _call(
        "PUT", "/prompts/QA_PRE_TRIAL", monkeypatch,
        scopes=(APIKeyScope.READ,), json={"content": "x"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_put_rejects_tasks_scope(monkeypatch):
    # Prompts are a single global registry driving every org's QA -- a
    # TASKS key (scoped to one org's tasks/trials) must not be able to
    # rewrite it. Only FULL may write.
    resp = await _call(
        "PUT", "/prompts/QA_PRE_TRIAL", monkeypatch,
        scopes=(APIKeyScope.TASKS,), json={"content": "x"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_put_accepts_full_scope(monkeypatch):
    async def fake_set(session, *, kind, content, description=None, created_by=None):
        return None

    monkeypatch.setattr(prompts_router, "set_prompt_core", fake_set)
    resp = await _call(
        "PUT", "/prompts/QA_PRE_TRIAL", monkeypatch,
        scopes=(APIKeyScope.FULL,), json={"content": "x"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_activate_route_is_gone(monkeypatch):
    # Latest-wins registry: there is no activation endpoint anymore.
    resp = await _call(
        "POST", "/prompts/QA_PRE_TRIAL/activate", monkeypatch,
        scopes=(APIKeyScope.FULL,), json={"version": 1},
    )
    assert resp.status_code in (404, 405)

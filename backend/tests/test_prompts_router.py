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
    async def fake_get(session, key, *, version=None):
        return _FakePrompt(), _FakeVersion(1, "hello")

    async def fake_usage(session, ref):
        return {"total": 0, "last_used_at": None, "by_version": []}

    monkeypatch.setattr(prompts_router, "get_session", lambda: _ctx(None))
    monkeypatch.setattr(prompts_router, "get_prompt_core", fake_get)
    monkeypatch.setattr(prompts_router, "get_prompt_usage_core", fake_usage)
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
async def test_get_prompt_returns_usage(monkeypatch):
    resp = await _call("GET", "/prompts/pre_trial_qa", monkeypatch)
    assert resp.status_code == 200
    assert resp.json()["usage"]["total"] == 0


@pytest.mark.asyncio
async def test_put_requires_write_scope(monkeypatch):
    # READ-only scope must be rejected on write
    resp = await _call(
        "PUT", "/prompts/pre_trial_qa", monkeypatch,
        scopes=(APIKeyScope.READ,), json={"content": "x"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_put_rejects_tasks_scope(monkeypatch):
    # Prompts are a single global registry driving every org's QA -- a
    # TASKS key (scoped to one org's tasks/trials) must not be able to
    # rewrite it. Only FULL may write.
    resp = await _call(
        "PUT", "/prompts/pre_trial_qa", monkeypatch,
        scopes=(APIKeyScope.TASKS,), json={"content": "x"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_put_accepts_full_scope(monkeypatch):
    async def fake_set(session, *, key, content, description=None, activate=True, created_by=None):
        return None

    monkeypatch.setattr(prompts_router, "set_prompt_core", fake_set)
    resp = await _call(
        "PUT", "/prompts/pre_trial_qa", monkeypatch,
        scopes=(APIKeyScope.FULL,), json={"content": "x"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_activate_rejects_tasks_scope(monkeypatch):
    resp = await _call(
        "POST", "/prompts/pre_trial_qa/activate", monkeypatch,
        scopes=(APIKeyScope.TASKS,), json={"version": 1},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_activate_accepts_full_scope(monkeypatch):
    async def fake_activate(session, key, version):
        return None

    monkeypatch.setattr(prompts_router, "activate_prompt_version_core", fake_activate)
    resp = await _call(
        "POST", "/prompts/pre_trial_qa/activate", monkeypatch,
        scopes=(APIKeyScope.FULL,), json={"version": 1},
    )
    assert resp.status_code == 200

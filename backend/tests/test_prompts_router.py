import contextlib

import pytest
from fastapi import HTTPException
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


_V1 = _FakeVersion(1, "hello")
_V2 = _FakeVersion(2, "world")


class _FakePrompt:
    id = "p1"
    kind = "QA_PRE_TRIAL"
    description = "d"
    created_at = __import__("datetime").datetime.now()
    updated_at = __import__("datetime").datetime.now()
    versions = [_V1, _V2]
    org_id = None


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


async def _call(
    method,
    path,
    monkeypatch,
    *,
    scopes=(APIKeyScope.READ,),
    prompt_org_id=None,
    prompt_core=None,
    **kwargs,
):
    async def fake_get(session, kind, *, version=None, scope_type=None, scope_id=None):
        if kind in {"not_a_kind", "NOT_A_KIND"}:
            raise HTTPException(status_code=404, detail="not found")
        prompt = _FakePrompt()
        prompt.org_id = prompt_org_id
        if version is not None:
            return prompt, _V1
        return prompt, _V2

    async def fake_usage(session, ref):
        return {"total": 0, "last_used_at": None, "by_version": []}

    monkeypatch.setattr(prompts_router, "get_session", lambda: _ctx(None))
    monkeypatch.setattr(prompts_router, "get_prompt_core", prompt_core or fake_get)
    monkeypatch.setattr(prompts_router, "get_prompt_usage_core", fake_usage)
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
    assert body["latest_version"] == 2
    assert body["version"] == 2
    assert body["content"] == "world"


@pytest.mark.asyncio
async def test_pinned_get_reports_true_latest(monkeypatch):
    # A version-pinned GET must still report the registry's true latest,
    # separately from the resolved (pinned) version.
    resp = await _call("GET", "/prompts/QA_PRE_TRIAL?version=1", monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_version"] == 2
    assert body["version"] == 1
    assert body["content"] == "hello"


@pytest.mark.asyncio
async def test_list_prompts_reports_latest_version(monkeypatch):
    async def fake_list(session, *, org_id=None):
        return [_FakePrompt()]

    monkeypatch.setattr(prompts_router, "get_session", lambda: _ctx(None))
    monkeypatch.setattr(prompts_router, "list_prompts_core", fake_list)
    app = create_app()
    app.dependency_overrides[require_auth] = _auth([APIKeyScope.READ])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/prompts")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["latest_version"] == 2


@pytest.mark.asyncio
async def test_get_unknown_kind_is_422(monkeypatch):
    # Neither a built-in PromptKind nor a lowercase-slug custom kind
    # (underscores/uppercase mixes are rejected) ever reaches core.
    resp = await _call("GET", "/prompts/not_a_kind", monkeypatch)
    assert resp.status_code == 422
    resp = await _call("GET", "/prompts/NOT_A_KIND", monkeypatch)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_custom_slug_kind_reaches_core(monkeypatch):
    # Lowercase-slug kinds are the custom-QA namespace (saved `oddish qa`
    # variants); they pass boundary validation and resolve through core.
    resp = await _call("GET", "/prompts/oracle-check", monkeypatch)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_existing_prompt_id_is_accepted(monkeypatch):
    resp = await _call("GET", "/prompts/p1", monkeypatch)
    assert resp.status_code == 200
    assert resp.json()["id"] == "p1"


@pytest.mark.asyncio
async def test_put_requires_write_scope(monkeypatch):
    # READ-only scope must be rejected on write
    resp = await _call(
        "PUT",
        "/prompts/QA_PRE_TRIAL",
        monkeypatch,
        scopes=(APIKeyScope.READ,),
        json={"content": "x"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_put_rejects_tasks_scope(monkeypatch):
    # Prompts are a single global registry driving every org's QA -- a
    # TASKS key (scoped to one org's tasks/trials) must not be able to
    # rewrite it. Only FULL may write.
    resp = await _call(
        "PUT",
        "/prompts/QA_PRE_TRIAL",
        monkeypatch,
        scopes=(APIKeyScope.TASKS,),
        json={"content": "x"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_put_accepts_full_scope(monkeypatch):
    async def fake_set(
        session,
        *,
        kind,
        content,
        description=None,
        created_by=None,
        scope_type=None,
        scope_id=None,
        org_id=None,
    ):
        return None

    monkeypatch.setattr(prompts_router, "set_prompt_core", fake_set)
    resp = await _call(
        "PUT",
        "/prompts/QA_PRE_TRIAL",
        monkeypatch,
        scopes=(APIKeyScope.FULL,),
        json={"content": "x"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_activate_route_is_gone(monkeypatch):
    # Latest-wins registry: there is no activation endpoint anymore.
    resp = await _call(
        "POST",
        "/prompts/QA_PRE_TRIAL/activate",
        monkeypatch,
        scopes=(APIKeyScope.FULL,),
        json={"version": 1},
    )
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_put_rejects_foreign_org_prompt_id(monkeypatch):
    # An id belonging to another org's prompt must 404 before ever reaching
    # set_prompt_core -- otherwise the write appends a version onto the
    # victim's row (see _validated_ref / _assert_org_access).
    called = False

    async def fake_set(session, **kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(prompts_router, "set_prompt_core", fake_set)
    resp = await _call(
        "PUT",
        "/prompts/p1",
        monkeypatch,
        scopes=(APIKeyScope.FULL,),
        prompt_org_id="org_2",
        json={"content": "x"},
    )
    assert resp.status_code == 404
    assert called is False


@pytest.mark.asyncio
async def test_get_rejects_foreign_org_prompt_id(monkeypatch):
    resp = await _call("GET", "/prompts/p1", monkeypatch, prompt_org_id="org_2")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_versions_rejects_foreign_org_prompt_id(monkeypatch):
    resp = await _call(
        "GET", "/prompts/p1/versions", monkeypatch, prompt_org_id="org_2"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_global_prompt_id_is_still_readable(monkeypatch):
    # org_id=None (global) must pass the guard untouched.
    resp = await _call("GET", "/prompts/p1", monkeypatch, prompt_org_id=None)
    assert resp.status_code == 200
    assert resp.json()["id"] == "p1"


@pytest.mark.asyncio
async def test_get_same_org_prompt_id_is_still_readable(monkeypatch):
    resp = await _call("GET", "/prompts/p1", monkeypatch, prompt_org_id="org_1")
    assert resp.status_code == 200
    assert resp.json()["id"] == "p1"


def _capturing_prompt_core(seen):
    async def fake_get(session, ref, *, version=None, scope_type=None, scope_id=None):
        # _validated_ref calls this first with no scope; record only the
        # router's own call, which is the second one.
        seen.append({"scope_type": scope_type, "scope_id": scope_id})
        return _FakePrompt(), _V2

    return fake_get


@pytest.mark.asyncio
async def test_get_prompt_passes_scope_to_core(monkeypatch):
    seen = []
    resp = await _call(
        "GET",
        "/prompts/QA_PRE_TRIAL?scope=task&scope_id=task_a",
        monkeypatch,
        prompt_core=_capturing_prompt_core(seen),
    )
    assert resp.status_code == 200
    assert seen[-1] == {"scope_type": "task", "scope_id": "task_a"}


@pytest.mark.asyncio
async def test_get_prompt_defaults_to_global_scope(monkeypatch):
    seen = []
    resp = await _call(
        "GET",
        "/prompts/QA_PRE_TRIAL",
        monkeypatch,
        prompt_core=_capturing_prompt_core(seen),
    )
    assert resp.status_code == 200
    assert seen[-1] == {"scope_type": None, "scope_id": None}


@pytest.mark.asyncio
async def test_get_prompt_org_scope_infers_auth_org(monkeypatch):
    seen = []
    resp = await _call(
        "GET",
        "/prompts/QA_PRE_TRIAL?scope=org",
        monkeypatch,
        prompt_core=_capturing_prompt_core(seen),
    )
    assert resp.status_code == 200
    # _auth() builds an AuthContext with org_id="org_1".
    assert seen[-1] == {"scope_type": "org", "scope_id": "org_1"}


@pytest.mark.asyncio
async def test_get_prompt_rejects_unknown_scope(monkeypatch):
    resp = await _call("GET", "/prompts/QA_PRE_TRIAL?scope=galaxy", monkeypatch)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_prompt_task_scope_requires_scope_id(monkeypatch):
    resp = await _call("GET", "/prompts/QA_PRE_TRIAL?scope=task", monkeypatch)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_set_prompt_rejects_unknown_scope(monkeypatch):
    resp = await _call(
        "PUT",
        "/prompts/QA_PRE_TRIAL?scope=galaxy",
        monkeypatch,
        scopes=(APIKeyScope.FULL,),
        json={"content": "x"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_set_prompt_domain_scope_requires_scope_id(monkeypatch):
    resp = await _call(
        "PUT",
        "/prompts/QA_PRE_TRIAL?scope=task",
        monkeypatch,
        scopes=(APIKeyScope.FULL,),
        json={"content": "x"},
    )
    assert resp.status_code == 422
    assert "scope_id" in resp.json()["detail"]


def _capturing_versions_core(seen):
    async def fake_versions(session, ref, *, scope_type=None, scope_id=None):
        seen.append({"scope_type": scope_type, "scope_id": scope_id})
        return [_V1, _V2]

    return fake_versions


@pytest.mark.asyncio
async def test_get_prompt_versions_passes_scope_to_core(monkeypatch):
    seen = []
    monkeypatch.setattr(
        prompts_router, "list_prompt_versions_core", _capturing_versions_core(seen)
    )
    resp = await _call(
        "GET", "/prompts/QA_PRE_TRIAL/versions?scope=task&scope_id=task_a", monkeypatch
    )
    assert resp.status_code == 200
    assert seen[-1] == {"scope_type": "task", "scope_id": "task_a"}


@pytest.mark.asyncio
async def test_get_prompt_versions_defaults_to_global_scope(monkeypatch):
    seen = []
    monkeypatch.setattr(
        prompts_router, "list_prompt_versions_core", _capturing_versions_core(seen)
    )
    resp = await _call("GET", "/prompts/QA_PRE_TRIAL/versions", monkeypatch)
    assert resp.status_code == 200
    assert seen[-1] == {"scope_type": None, "scope_id": None}

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import cost_excluded_models as router_mod
from auth import AuthContext, AuthMethod, require_auth
from models import APIKeyScope, UserRole
from oddish.db import CostExcludedModelModel, utcnow

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org_1")


class _FakeScalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    def __init__(self, results=()):
        self.results = [list(rows) for rows in results]
        self.added: list[object] = []
        self.committed = False

    async def scalars(self, _stmt):
        return _FakeScalars(self.results.pop(0) if self.results else [])

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added.extend(objs)

    async def commit(self):
        from oddish.db import generate_id

        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = generate_id()
            if getattr(obj, "created_at", None) is None:
                obj.created_at = utcnow()
        self.committed = True


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


def _install_fake_get_session(monkeypatch, session):
    monkeypatch.setattr(router_mod, "get_session", lambda: _FakeSessionCtx(session))


def _admin_jwt() -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_1",
        user_id="admin_1",
        user_role=UserRole.ADMIN,
    )


def _member_jwt() -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_1",
        user_id="member_1",
        user_role=UserRole.MEMBER,
    )


def _other_admin_jwt() -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_2",
        user_id="admin_2",
        user_role=UserRole.ADMIN,
    )


def _full_api_key() -> AuthContext:
    return AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="key_1",
        user_role=UserRole.ADMIN,
        scope=APIKeyScope.FULL,
    )


@pytest.fixture
def app():
    return create_app()


def _client(app, auth):
    app.dependency_overrides[require_auth] = lambda: auth
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture
async def admin_client(app):
    app.dependency_overrides[require_auth] = lambda: _admin_jwt()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(require_auth, None)


async def test_add_stores_the_spelling_trials_actually_use(admin_client, monkeypatch):
    session = FakeSession(results=[["moonshot/kimi-k2"], []])
    _install_fake_get_session(monkeypatch, session)

    resp = await admin_client.post(
        "/admin/cost-excluded-models",
        json={"model_name": "  Kimi-K2  ", "label": "sponsored"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["model_name"] for r in body] == ["moonshot/kimi-k2"]
    assert body[0]["label"] == "sponsored"
    assert session.added[0].model_name == "moonshot/kimi-k2"
    assert session.committed


async def test_add_covers_every_stored_spelling(admin_client, monkeypatch):
    session = FakeSession(results=[["grok-free-preview", "xai/grok-free-preview"], []])
    _install_fake_get_session(monkeypatch, session)

    resp = await admin_client.post(
        "/admin/cost-excluded-models", json={"model_name": "grok-free-preview"}
    )
    assert resp.status_code == 200, resp.text
    assert sorted(r["model_name"] for r in resp.json()) == [
        "grok-free-preview",
        "xai/grok-free-preview",
    ]


async def test_add_prefixed_id_covers_the_bare_spelling(admin_client, monkeypatch):
    session = FakeSession(results=[["grok-free-preview", "xai/grok-free-preview"], []])
    _install_fake_get_session(monkeypatch, session)

    resp = await admin_client.post(
        "/admin/cost-excluded-models", json={"model_name": "xai/grok-free-preview"}
    )
    assert resp.status_code == 200, resp.text
    assert sorted(r["model_name"] for r in resp.json()) == [
        "grok-free-preview",
        "xai/grok-free-preview",
    ]


async def test_add_unknown_model_is_404(admin_client, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(results=[[]]))
    resp = await admin_client.post(
        "/admin/cost-excluded-models", json={"model_name": "never/ran"}
    )
    assert resp.status_code == 404


async def test_add_duplicate_is_409(admin_client, monkeypatch):
    _install_fake_get_session(
        monkeypatch, FakeSession(results=[["xai/grok-4"], ["xai/grok-4"]])
    )
    resp = await admin_client.post(
        "/admin/cost-excluded-models", json={"model_name": "xai/grok-4"}
    )
    assert resp.status_code == 409


async def test_add_race_integrity_error_is_409(admin_client, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    class RacingSession(FakeSession):
        async def commit(self):
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    _install_fake_get_session(monkeypatch, RacingSession(results=[["xai/grok-4"], []]))
    resp = await admin_client.post(
        "/admin/cost-excluded-models", json={"model_name": "xai/grok-4"}
    )
    assert resp.status_code == 409


@pytest.mark.parametrize("value", ["", "   ", "/"])
async def test_add_empty_model_is_400(admin_client, monkeypatch, value):
    _install_fake_get_session(monkeypatch, FakeSession())
    resp = await admin_client.post(
        "/admin/cost-excluded-models", json={"model_name": value}
    )
    assert resp.status_code == 400


async def test_list_returns_rows(admin_client, monkeypatch):
    row = CostExcludedModelModel(
        id="m1", model_name="xai/grok-4", label="sponsored", created_at=utcnow()
    )
    _install_fake_get_session(monkeypatch, FakeSession(results=[[row]]))
    resp = await admin_client.get("/admin/cost-excluded-models")
    assert resp.status_code == 200
    assert resp.json()[0]["model_name"] == "xai/grok-4"


async def test_delete_soft_deletes(admin_client, monkeypatch):
    row = CostExcludedModelModel(id="m1", model_name="xai/grok-4", label="")
    session = FakeSession(results=[[row]])
    _install_fake_get_session(monkeypatch, session)
    resp = await admin_client.delete("/admin/cost-excluded-models/m1")
    assert resp.status_code == 200
    assert row.deleted_at is not None
    assert session.committed


async def test_delete_not_found_is_404(admin_client, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(results=[[]]))
    resp = await admin_client.delete("/admin/cost-excluded-models/missing")
    assert resp.status_code == 404


async def test_member_jwt_cannot_add(app, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession())
    client = _client(app, _member_jwt())
    try:
        resp = await client.post(
            "/admin/cost-excluded-models", json={"model_name": "xai/grok-4"}
        )
        assert resp.status_code == 403
    finally:
        await client.aclose()
        app.dependency_overrides.pop(require_auth, None)


async def test_operator_full_api_key_can_add(app, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(results=[["xai/grok-4"], []]))
    client = _client(app, _full_api_key())
    try:
        resp = await client.post(
            "/admin/cost-excluded-models", json={"model_name": "xai/grok-4"}
        )
        assert resp.status_code == 200, resp.text
    finally:
        await client.aclose()
        app.dependency_overrides.pop(require_auth, None)


@pytest.mark.parametrize("method", ["get", "post", "delete"])
async def test_non_operator_admin_cannot_access(app, monkeypatch, method):
    _install_fake_get_session(monkeypatch, FakeSession())
    client = _client(app, _other_admin_jwt())
    try:
        if method == "get":
            resp = await client.get("/admin/cost-excluded-models")
        elif method == "post":
            resp = await client.post(
                "/admin/cost-excluded-models", json={"model_name": "xai/grok-4"}
            )
        else:
            resp = await client.delete("/admin/cost-excluded-models/m1")
        assert resp.status_code == 403
    finally:
        await client.aclose()
        app.dependency_overrides.pop(require_auth, None)

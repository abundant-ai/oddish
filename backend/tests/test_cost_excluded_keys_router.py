"""HTTP-level tests for /admin/cost-excluded-keys.

No live Postgres: the real app is built via create_app() but get_session is
monkeypatched to a fake async session, and require_auth is overridden per auth
scenario. Covers that the pasted key is hashed and discarded, and that only a
JWT org admin may edit the list.
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import cost_excluded_keys as router_mod
from auth import AuthContext, AuthMethod, require_auth
from models import APIKeyScope, UserRole

pytestmark = pytest.mark.asyncio


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    def __init__(self, query_rows=()):
        self.query_rows = list(query_rows)
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _stmt):
        return _FakeResult(self.query_rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        # Simulate the Python-side column defaults a real flush would apply.
        from oddish.db import generate_id, utcnow

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


async def _client(app, auth):
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


async def test_add_hashes_key_and_discards_plaintext(admin_client, monkeypatch):
    session = FakeSession(query_rows=[])  # no existing row
    _install_fake_get_session(monkeypatch, session)

    resp = await admin_client.post(
        "/admin/cost-excluded-keys",
        json={"key": "xai-super-secret-9f2c", "label": "sponsored"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key_hint"] == "9f2c"
    assert body["label"] == "sponsored"
    # No plaintext or hash leaks in the response.
    assert "key" not in body and "key_hash" not in body

    assert len(session.added) == 1
    row = session.added[0]
    assert row.key_hash == hashlib.sha256(b"xai-super-secret-9f2c").hexdigest()
    assert row.key_hint == "9f2c"
    assert "xai-super-secret-9f2c" not in (row.key_hash, row.key_hint)
    assert session.committed


async def test_add_duplicate_is_409(admin_client, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(query_rows=[object()]))
    resp = await admin_client.post(
        "/admin/cost-excluded-keys", json={"key": "xai-dupe-key"}
    )
    assert resp.status_code == 409


async def test_add_empty_key_is_400(admin_client, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(query_rows=[]))
    resp = await admin_client.post("/admin/cost-excluded-keys", json={"key": "   "})
    assert resp.status_code == 400


async def test_list_returns_hints(admin_client, monkeypatch):
    from oddish.db import CostExcludedLlmKeyModel, utcnow

    row = CostExcludedLlmKeyModel(
        id="k1", key_hash="h", key_hint="9f2c", label="sponsored", created_at=utcnow()
    )
    _install_fake_get_session(monkeypatch, FakeSession(query_rows=[row]))
    resp = await admin_client.get("/admin/cost-excluded-keys")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["key_hint"] == "9f2c"
    assert "key_hash" not in body[0]


async def test_delete_soft_deletes(admin_client, monkeypatch):
    from oddish.db import CostExcludedLlmKeyModel

    row = CostExcludedLlmKeyModel(id="k1", key_hash="h", key_hint="9f2c", label="x")
    session = FakeSession(query_rows=[row])
    _install_fake_get_session(monkeypatch, session)
    resp = await admin_client.delete("/admin/cost-excluded-keys/k1")
    assert resp.status_code == 200
    assert row.deleted_at is not None
    assert session.committed


async def test_delete_not_found_is_404(admin_client, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(query_rows=[]))
    resp = await admin_client.delete("/admin/cost-excluded-keys/missing")
    assert resp.status_code == 404


async def test_member_jwt_cannot_add(app, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(query_rows=[]))
    client = await _client(app, _member_jwt())
    try:
        resp = await client.post(
            "/admin/cost-excluded-keys", json={"key": "xai-key"}
        )
        assert resp.status_code == 403
    finally:
        await client.aclose()
        app.dependency_overrides.pop(require_auth, None)


async def test_full_api_key_cannot_add(app, monkeypatch):
    # A FULL-scope API key passes require_admin but must not edit what counts as
    # spend, so the manage gate rejects it.
    _install_fake_get_session(monkeypatch, FakeSession(query_rows=[]))
    client = await _client(app, _full_api_key())
    try:
        resp = await client.post(
            "/admin/cost-excluded-keys", json={"key": "xai-key"}
        )
        assert resp.status_code == 403
    finally:
        await client.aclose()
        app.dependency_overrides.pop(require_auth, None)

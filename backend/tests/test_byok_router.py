"""HTTP-level tests for the /byok settings API (backend/api/routers/byok.py).

This environment has no live Postgres, so the suite builds the real app via
create_app() but never lets a request touch the database: `get_session` is
monkeypatched to a fake async session/result, `require_auth` is overridden per
auth scenario, and the Statsig gate check is monkeypatched.
"""

from __future__ import annotations

import base64
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import crypto
import statsig_client
from api.app import create_app
from api.routers import byok as byok_router
from auth import AuthContext, AuthMethod, require_auth

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _enc_key(monkeypatch):
    monkeypatch.setenv("ODDISH_CRED_ENC_KEY", base64.b64encode(os.urandom(32)).decode())
    crypto.reset_key_cache()
    monkeypatch.setattr(statsig_client, "byok_gate_passes", lambda *a, **k: False)
    yield
    crypto.reset_key_cache()


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, query_rows=()):
        self.query_rows = list(query_rows)
        self.ops: list[tuple] = []
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _stmt):
        self.ops.append(("execute",))
        return _FakeResult(self.query_rows)

    def add(self, obj):
        self.ops.append(("add", obj))
        self.added.append(obj)

    async def flush(self):
        self.ops.append(("flush",))

    async def commit(self):
        self.ops.append(("commit",))
        self.committed = True


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


def _install_fake_get_session(monkeypatch, session):
    monkeypatch.setattr(byok_router, "get_session", lambda: _FakeSessionCtx(session))


def _user_auth() -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_1",
        user_id="user_1",
        user_email="member@example.com",
    )


def _api_key_auth() -> AuthContext:
    return AuthContext(method=AuthMethod.API_KEY, org_id="org_1", user_id="creator_1")


@pytest.fixture
def app():
    return create_app()


@pytest_asyncio.fixture
async def client_user_auth(app):
    app.dependency_overrides[require_auth] = lambda: _user_auth()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(require_auth, None)


@pytest_asyncio.fixture
async def client_api_key_auth(app):
    app.dependency_overrides[require_auth] = lambda: _api_key_auth()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(require_auth, None)


async def test_api_key_auth_rejected(client_api_key_auth):
    assert (await client_api_key_auth.get("/byok")).status_code == 403


async def test_get_status_no_key(client_user_auth, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(query_rows=[]))
    resp = await client_user_auth.get("/byok")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"enabled": False, "key_set": False, "key_hint": ""}


async def test_put_key_encrypts_and_stores(client_user_auth, monkeypatch):
    session = FakeSession(query_rows=[])
    _install_fake_get_session(monkeypatch, session)
    resp = await client_user_auth.put(
        "/byok/keys/anthropic", json={"key": "sk-ant-secret2345"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["key_set"] is True
    assert resp.json()["key_hint"] == "2345"
    assert len(session.added) == 1
    stored = session.added[0]
    assert stored.vendor == "anthropic"
    # The plaintext never lands in the row; the ciphertext round-trips back.
    assert stored.ciphertext != b"sk-ant-secret2345"
    assert crypto.decrypt_secret(stored.ciphertext, stored.key_version) == "sk-ant-secret2345"


async def test_put_empty_key_rejected(client_user_auth):
    resp = await client_user_auth.put("/byok/keys/anthropic", json={"key": "   "})
    assert resp.status_code == 400


async def test_put_without_enc_key_returns_503(client_user_auth, monkeypatch):
    monkeypatch.delenv("ODDISH_CRED_ENC_KEY", raising=False)
    crypto.reset_key_cache()
    resp = await client_user_auth.put(
        "/byok/keys/anthropic", json={"key": "sk-ant-whatever"}
    )
    assert resp.status_code == 503


async def test_delete_no_key_is_404(client_user_auth, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(query_rows=[]))
    assert (await client_user_auth.delete("/byok/keys/anthropic")).status_code == 404

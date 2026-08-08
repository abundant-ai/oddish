from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import cost_excluded_experiments as router_mod
from auth import AuthContext, AuthMethod, require_auth
from models import APIKeyScope, UserRole
from oddish.db import ExperimentModel

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org_1")


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
    """Returns one queued result list per execute() call, in order."""

    def __init__(self, results=()):
        self.results = [list(rows) for rows in results]
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _stmt):
        return _FakeResult(self.results.pop(0) if self.results else [])

    def add(self, obj):
        self.added.append(obj)

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


def _experiment(exp_id="exp_1", name="glm sweep") -> ExperimentModel:
    return ExperimentModel(id=exp_id, name=name)


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


async def test_add_by_id_snapshots_name(admin_client, monkeypatch):
    # Queries: resolve by id (hit), duplicate check (miss).
    session = FakeSession(results=[[_experiment()], []])
    _install_fake_get_session(monkeypatch, session)

    resp = await admin_client.post(
        "/admin/cost-excluded-experiments",
        json={"experiment": "exp_1", "label": "sponsored"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["experiment_id"] == "exp_1"
    assert body["experiment_name"] == "glm sweep"
    assert body["label"] == "sponsored"

    assert len(session.added) == 1
    row = session.added[0]
    assert row.experiment_id == "exp_1"
    assert row.experiment_name == "glm sweep"
    assert session.committed


async def test_add_by_name_resolves(admin_client, monkeypatch):
    # Queries: resolve by id (miss), by name (hit), duplicate check (miss).
    session = FakeSession(results=[[], [_experiment()], []])
    _install_fake_get_session(monkeypatch, session)

    resp = await admin_client.post(
        "/admin/cost-excluded-experiments", json={"experiment": "glm sweep"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["experiment_id"] == "exp_1"


async def test_add_ambiguous_name_is_409(admin_client, monkeypatch):
    session = FakeSession(
        results=[[], [_experiment("exp_1"), _experiment("exp_2")]]
    )
    _install_fake_get_session(monkeypatch, session)

    resp = await admin_client.post(
        "/admin/cost-excluded-experiments", json={"experiment": "glm sweep"}
    )
    assert resp.status_code == 409
    assert "ambiguous" in resp.json()["detail"]


async def test_add_unknown_experiment_is_404(admin_client, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(results=[[], []]))
    resp = await admin_client.post(
        "/admin/cost-excluded-experiments", json={"experiment": "missing"}
    )
    assert resp.status_code == 404


async def test_add_duplicate_is_409(admin_client, monkeypatch):
    session = FakeSession(results=[[_experiment()], [object()]])
    _install_fake_get_session(monkeypatch, session)
    resp = await admin_client.post(
        "/admin/cost-excluded-experiments", json={"experiment": "exp_1"}
    )
    assert resp.status_code == 409


async def test_add_race_integrity_error_is_409(admin_client, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    class RacingSession(FakeSession):
        async def commit(self):
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    _install_fake_get_session(
        monkeypatch, RacingSession(results=[[_experiment()], []])
    )
    resp = await admin_client.post(
        "/admin/cost-excluded-experiments", json={"experiment": "exp_1"}
    )
    assert resp.status_code == 409


async def test_add_empty_experiment_is_400(admin_client, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession())
    resp = await admin_client.post(
        "/admin/cost-excluded-experiments", json={"experiment": "   "}
    )
    assert resp.status_code == 400


async def test_list_returns_rows(admin_client, monkeypatch):
    from oddish.db import CostExcludedExperimentModel, utcnow

    row = CostExcludedExperimentModel(
        id="x1",
        experiment_id="exp_1",
        experiment_name="glm sweep",
        label="sponsored",
        created_at=utcnow(),
    )
    _install_fake_get_session(monkeypatch, FakeSession(results=[[row]]))
    resp = await admin_client.get("/admin/cost-excluded-experiments")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["experiment_id"] == "exp_1"
    assert body[0]["experiment_name"] == "glm sweep"


async def test_delete_soft_deletes(admin_client, monkeypatch):
    from oddish.db import CostExcludedExperimentModel

    row = CostExcludedExperimentModel(
        id="x1", experiment_id="exp_1", experiment_name="glm sweep", label=""
    )
    session = FakeSession(results=[[row]])
    _install_fake_get_session(monkeypatch, session)
    resp = await admin_client.delete("/admin/cost-excluded-experiments/x1")
    assert resp.status_code == 200
    assert row.deleted_at is not None
    assert session.committed


async def test_delete_not_found_is_404(admin_client, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession(results=[[]]))
    resp = await admin_client.delete("/admin/cost-excluded-experiments/missing")
    assert resp.status_code == 404


async def test_member_jwt_cannot_add(app, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession())
    client = _client(app, _member_jwt())
    try:
        resp = await client.post(
            "/admin/cost-excluded-experiments", json={"experiment": "exp_1"}
        )
        assert resp.status_code == 403
    finally:
        await client.aclose()
        app.dependency_overrides.pop(require_auth, None)


async def test_full_api_key_cannot_add(app, monkeypatch):
    _install_fake_get_session(monkeypatch, FakeSession())
    client = _client(app, _full_api_key())
    try:
        resp = await client.post(
            "/admin/cost-excluded-experiments", json={"experiment": "exp_1"}
        )
        assert resp.status_code == 403
    finally:
        await client.aclose()
        app.dependency_overrides.pop(require_auth, None)


@pytest.mark.parametrize("method", ["get", "post", "delete"])
async def test_non_operator_admin_cannot_access_list(app, monkeypatch, method):
    _install_fake_get_session(monkeypatch, FakeSession())
    client = _client(app, _other_admin_jwt())
    try:
        if method == "get":
            resp = await client.get("/admin/cost-excluded-experiments")
        elif method == "post":
            resp = await client.post(
                "/admin/cost-excluded-experiments", json={"experiment": "exp_1"}
            )
        else:
            resp = await client.delete("/admin/cost-excluded-experiments/x1")
        assert resp.status_code == 403
    finally:
        await client.aclose()
        app.dependency_overrides.pop(require_auth, None)

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers import admin as admin_router
from auth import require_admin
from oddish.config import settings


class _Session:
    def __init__(self, *, overrides=None, advisory=None):
        self.calls = []
        self.overrides = dict(overrides or {})
        self.advisory = dict(advisory or {})

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.calls.append((sql, params))
        if "INSERT INTO model_concurrency_overrides" in sql:
            self.overrides[params["queue_key"]] = params["concurrency_limit"]
        elif "DELETE FROM model_concurrency_overrides" in sql:
            self.overrides.pop(params["queue_key"], None)
        elif "FROM model_concurrency_overrides" in sql:
            queue_keys = params.get("queue_keys", self.overrides)
            rows = [
                SimpleNamespace(queue_key=key, concurrency_limit=self.overrides[key])
                for key in queue_keys
                if key in self.overrides
            ]
            return SimpleNamespace(all=lambda: rows)
        elif "FROM   model_concurrency_advisory" in sql:
            rows = [
                SimpleNamespace(queue_key=key, advisory_limit=value)
                for key, value in self.advisory.items()
            ]
            return SimpleNamespace(all=lambda: rows)
        return SimpleNamespace(all=lambda: [])


@asynccontextmanager
async def _fake_session(session):
    yield session


def _app():
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(org_id="org-1")
    return app


@pytest.fixture(autouse=True)
def operator_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-1")
    monkeypatch.setattr(settings, "dynamic_model_concurrency", False)


@pytest.mark.asyncio
async def test_admin_update_normalizes_key_and_reports_both_limits(monkeypatch):
    session = _Session()
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "MiniMax/MiniMax-M3", "limit": 96},
        )

    assert response.status_code == 200
    assert response.json() == {
        "queue_key": "minimax/minimax-m3",
        "limit": 96,
        "deploy_limit": settings.get_model_concurrency("minimax/minimax-m3"),
        "override_limit": 96,
        "controller_enabled": False,
        "advisory_limit": None,
        "effective_limit": 96,
    }
    statement, params = session.calls[0]
    assert "INSERT INTO model_concurrency_overrides" in statement
    assert params == {"queue_key": "minimax/minimax-m3", "concurrency_limit": 96}


@pytest.mark.asyncio
async def test_admin_clearing_an_override_falls_back_to_the_deploy_limit(monkeypatch):
    session = _Session()
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))
    deploy_limit = settings.get_model_concurrency("minimax/minimax-m3")

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": None},
        )

    assert response.status_code == 200
    assert response.json() == {
        "queue_key": "minimax/minimax-m3",
        "limit": deploy_limit,
        "deploy_limit": deploy_limit,
        "override_limit": None,
        "controller_enabled": False,
        "advisory_limit": None,
        "effective_limit": deploy_limit,
    }
    assert "DELETE FROM model_concurrency_overrides" in session.calls[0][0]


@pytest.mark.asyncio
async def test_admin_get_normalizes_key_and_reads_database_override(monkeypatch):
    session = _Session(overrides={"minimax/minimax-m3": 96})
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/admin/concurrency",
            params={"queue_key": " MiniMax/MiniMax-M3 "},
        )

    assert response.status_code == 200
    assert response.json() == {
        "queue_key": "minimax/minimax-m3",
        "limit": 96,
        "deploy_limit": settings.get_model_concurrency("minimax/minimax-m3"),
        "override_limit": 96,
        "controller_enabled": False,
        "advisory_limit": None,
        "effective_limit": 96,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("advisory", "effective"), [(80, 80), (120, 96)])
async def test_admin_get_reports_fresh_dynamic_advisory(
    monkeypatch, advisory, effective
):
    session = _Session(
        overrides={"minimax/minimax-m3": 96},
        advisory={"MiniMax/MiniMax-M3": advisory},
    )
    monkeypatch.setattr(admin_router, "get_session", lambda: _fake_session(session))
    monkeypatch.setattr(settings, "dynamic_model_concurrency", True)

    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/admin/concurrency",
            params={"queue_key": "minimax/minimax-m3"},
        )

    assert response.status_code == 200
    assert response.json()["controller_enabled"] is True
    assert response.json()["advisory_limit"] == advisory
    assert response.json()["effective_limit"] == effective


@pytest.mark.asyncio
async def test_admin_get_rejects_blank_queue_key():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/admin/concurrency", params={"queue_key": "   "})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_invalid_limit():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": 10_001},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [True, False, 96.0, "96"])
async def test_admin_concurrency_requires_an_integer_json_limit(limit):
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": limit},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_blank_queue_key():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency", json={"queue_key": "   ", "limit": 0}
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_a_misspelled_limit_field():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limlt": 96},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_concurrency_requires_admin():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": 96},
        )

    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_admin_concurrency_get_requires_admin():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/admin/concurrency",
            params={"queue_key": "minimax/minimax-m3"},
        )

    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_admin_concurrency_rejects_other_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-2")
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.put(
            "/admin/concurrency",
            json={"queue_key": "minimax/minimax-m3", "limit": 96},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_concurrency_get_rejects_other_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-2")
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/admin/concurrency",
            params={"queue_key": "minimax/minimax-m3"},
        )

    assert response.status_code == 403

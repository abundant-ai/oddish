from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.routers import admin


@asynccontextmanager
async def _session():
    yield object()


@pytest.mark.asyncio
async def test_admin_diagnostics_pass_active_org(monkeypatch):
    auth = SimpleNamespace(org_id="org-a")
    monkeypatch.setattr(admin, "get_session", _session)

    calls = []

    async def fake(session, **kwargs):
        calls.append(kwargs)
        return object()

    for route, core_name, kwargs in (
        (admin.get_queue_status, "get_queue_status_core", {}),
        (admin.get_queue_health, "get_queue_health_core", {}),
        (
            admin.get_orphaned_state,
            "get_orphaned_state_core",
            {"stale_after_minutes": 10},
        ),
        (
            admin.get_worker_jobs,
            "get_worker_jobs_admin_core",
            {"stale_after_minutes": 10, "sample_limit": 5},
        ),
    ):
        monkeypatch.setattr(admin, core_name, fake)
        await route(auth=auth, **kwargs)

    assert calls == [
        {"org_id": "org-a"},
        {"org_id": "org-a", "include_global_details": False},
        {"stale_after_minutes": 10, "org_id": "org-a"},
        {"stale_after_minutes": 10, "sample_limit": 5, "org_id": "org-a"},
    ]


@pytest.mark.asyncio
async def test_queue_slots_require_operator_org(monkeypatch):
    monkeypatch.delenv("ODDISH_OPERATOR_ORG_ID", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        await admin.get_queue_slots(auth=SimpleNamespace(org_id="org-a"))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_costs_pass_active_org(monkeypatch):
    auth = SimpleNamespace(org_id="org-a")
    result = object()
    seen = {}
    monkeypatch.setattr(admin, "get_session", _session)

    async def fake_costs(session, **kwargs):
        seen.update(kwargs)
        return result

    async def fake_enrich(session, value, **kwargs):
        assert value is result
        seen["enriched_org_id"] = kwargs["org_id"]

    monkeypatch.setattr(admin, "get_cost_breakdown_core", fake_costs)
    monkeypatch.setattr(admin, "_enrich_cost_breakdown", fake_enrich)

    response = await admin.get_costs(
        auth=auth, window_days=7, experiment_limit=10, user_limit=20
    )

    assert response is result
    assert seen == {
        "org_id": "org-a",
        "window_days": 7,
        "experiment_limit": 10,
        "user_limit": 20,
        "resolve_github_users": admin.resolve_github_users,
        "enriched_org_id": "org-a",
    }


@pytest.mark.asyncio
async def test_operator_queue_health_includes_global_details(monkeypatch):
    seen = {}
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-a")
    monkeypatch.setattr(admin, "get_session", _session)

    async def fake(session, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(admin, "get_queue_health_core", fake)
    await admin.get_queue_health(auth=SimpleNamespace(org_id="org-a"))

    assert seen == {"org_id": "org-a", "include_global_details": True}


@pytest.mark.asyncio
async def test_operator_access_is_bound_to_configured_org(monkeypatch):
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-a")
    allowed = await admin.get_operator_access(auth=SimpleNamespace(org_id="org-a"))
    denied = await admin.get_operator_access(auth=SimpleNamespace(org_id="org-b"))

    monkeypatch.delenv("ODDISH_OPERATOR_ORG_ID")
    unconfigured = await admin.get_operator_access(auth=SimpleNamespace(org_id="org-a"))

    assert allowed.allowed is True
    assert denied.allowed is False
    assert unconfigured.allowed is False

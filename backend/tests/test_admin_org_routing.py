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
    monkeypatch.delenv("ODDISH_OPERATOR_ORG_ID", raising=False)
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
async def test_operator_queue_diagnostics_include_global_details(monkeypatch):
    calls = []
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org-a")
    monkeypatch.setattr(admin, "get_session", _session)

    async def fake(session, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(admin, "get_queue_health_core", fake)
    monkeypatch.setattr(admin, "get_queue_status_core", fake)
    await admin.get_queue_health(auth=SimpleNamespace(org_id="org-a"))
    await admin.get_queue_status(auth=SimpleNamespace(org_id="org-a"))

    assert calls == [
        {"org_id": None, "include_global_details": True},
        {"org_id": None},
    ]


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


def test_operator_org_slug_prefix(monkeypatch):
    from auth.permissions import is_operator_org

    # "slug:" prefix matches the org slug (case-insensitive), regardless of the
    # opaque internal id.
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "slug:abundant")
    assert is_operator_org(SimpleNamespace(org_id="org_9f3a", org_slug="abundant"))
    assert is_operator_org(SimpleNamespace(org_id="org_9f3a", org_slug="Abundant"))
    assert not is_operator_org(SimpleNamespace(org_id="org_9f3a", org_slug="acme"))
    # A SimpleNamespace without org_slug must not blow up (getattr fallback).
    assert not is_operator_org(SimpleNamespace(org_id="org_9f3a"))


def test_operator_org_bare_value_is_id_only(monkeypatch):
    from auth.permissions import is_operator_org

    # A bare (unprefixed) value matches the server-issued org id only.
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org_9f3a")
    assert is_operator_org(SimpleNamespace(org_id="org_9f3a", org_slug="anything"))

    # Escalation guard: a tenant that claims the operator's id-string as its own
    # slug must NOT gain operator access (org_id differs).
    assert not is_operator_org(SimpleNamespace(org_id="org_evil", org_slug="org_9f3a"))

    # Blank/unset config grants operator access to no one.
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "   ")
    assert not is_operator_org(SimpleNamespace(org_id="org_9f3a", org_slug="abundant"))
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "slug:   ")
    assert not is_operator_org(SimpleNamespace(org_id="org_9f3a", org_slug=""))

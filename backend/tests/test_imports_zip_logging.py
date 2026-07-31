"""``/imports/zip`` must leave a diagnosable trail.

Two failure shapes have to be tellable apart from the Modal logs alone:

- the endpoint raised -- the traceback plus the upload's shape must be
  logged, and the caller must get an honest message rather than
  Starlette's opaque "Internal Server Error";
- the container died mid-request (OOM) -- no traceback exists to log, so
  the *entry* breadcrumb carrying the byte sizes is the only evidence of
  what was being processed.
"""

from __future__ import annotations

import logging
import os

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import UserRole


def _client_app():
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=f"org-imp-{os.getpid()}",
        user_role=UserRole.MEMBER,
    )
    return app


def _files():
    return {"task_zip": ("my-task.zip", b"PK\x03\x04padding", "application/zip")}


@pytest.mark.asyncio
async def test_import_zip_failure_is_logged_with_context(monkeypatch, caplog):
    import api.routers.imports as imports_router

    async def boom(**kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(imports_router, "import_zip", boom)

    app = _client_app()
    try:
        with caplog.at_level(logging.ERROR, logger="api.routers.imports"):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/imports/zip", files=_files())
    finally:
        app.dependency_overrides.clear()

    # The caller learns something real, not "Internal Server Error".
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "RuntimeError" in detail

    records = [r for r in caplog.records if r.name == "api.routers.imports"]
    assert records, "the failure was not logged at all"
    failure = records[-1]
    assert failure.exc_info is not None, "logged without a traceback"
    assert "my-task.zip" in failure.getMessage()


@pytest.mark.asyncio
async def test_deliberate_http_errors_pass_through_untouched(monkeypatch):
    """The catch-all must not rewrite the import's own 4xx/5xx verdicts."""
    import api.routers.imports as imports_router

    async def not_found(**kwargs):
        raise HTTPException(status_code=404, detail="No task named 'nope'")

    monkeypatch.setattr(imports_router, "import_zip", not_found)

    app = _client_app()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/imports/zip", files=_files())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "No task named 'nope'"


@pytest.mark.asyncio
async def test_missing_both_zips_still_400(monkeypatch):
    app = _client_app()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/imports/zip", data={"message": "hi"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_import_zip_logs_upload_shape_before_importing(monkeypatch, caplog):
    """The breadcrumb that survives an OOM kill: sizes, logged up front."""
    import api.routers.imports as imports_router

    async def ok(**kwargs):
        return object()

    monkeypatch.setattr(imports_router, "import_zip", ok)
    monkeypatch.setattr(
        imports_router, "summarize_response", lambda result: {"ok": True}
    )

    app = _client_app()
    try:
        with caplog.at_level(logging.INFO, logger="api.routers.imports"):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/imports/zip", files=_files())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    messages = [
        r.getMessage() for r in caplog.records if r.name == "api.routers.imports"
    ]
    entry = [m for m in messages if "my-task.zip" in m]
    assert entry, f"no entry breadcrumb naming the upload; saw {messages}"
    # Byte count of the stashed archive, so an OOM can be attributed to size.
    assert str(len(b"PK\x03\x04padding")) in entry[0]

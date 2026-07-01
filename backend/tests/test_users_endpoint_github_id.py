"""G6: GET /users surfaces github_id alongside github_username.

Exercises the real endpoint over ASGI with a FULL-scope org API key
(`require_admin` authorizes FULL keys by scope, auth/__init__.py:305-307), so
the full `select(UserModel)` query + `UserResponse` serialization run end to end.

`list_users` uses a plain `select(UserModel)` with NO `load_only`, so the
`MissingGreenlet` deferred-column trap (CLAUDE.md) does not apply here — reading
`u.github_id` in the builder is safe. github_id is a USERS column and is not
surfaced through any compact task/trial/experiment response.

Run with your backend env sourced:

    export ODDISH_DATABASE_URL=postgresql+asyncpg://oddish:oddish@localhost:5432/oddish
    uv run pytest tests/test_users_endpoint_github_id.py -q
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.schemas import UserResponse
from models import APIKeyScope, OrganizationModel, UserModel
from oddish.core.api_keys import create_api_key
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _new_user(org_id: str, handle: str, *, github_id: str | None = None) -> UserModel:
    suffix = uuid.uuid4().hex[:8]
    return UserModel(
        id=f"user_{suffix}",
        org_id=org_id,
        email=f"{handle}_{suffix}@example.com",
        github_username=handle,
        github_id=github_id,
        clerk_user_id=f"clerk_{suffix}",
        role="member",
        is_active=True,
    )


async def _purge(*, org_ids, user_ids, api_key_ids) -> None:
    from oddish.db.models import APIKeyModel

    async with get_session() as session:
        if user_ids:
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id.in_(user_ids))
            )
        if api_key_ids:
            await session.execute(
                APIKeyModel.__table__.delete().where(APIKeyModel.id.in_(api_key_ids))
            )
        if org_ids:
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id.in_(org_ids)
                )
            )


@pytest_asyncio.fixture
async def org_with_full_key():
    """Org + a FULL-scope API key (authorizes `require_admin`) + two members:
    one with github_id set, one without. Yields (org_id, raw_key, with_id, no_id).
    """
    org_id = f"org_g6_{uuid.uuid4().hex[:8]}"
    with_id = _new_user(org_id, "hasid", github_id="9999001")
    no_id = _new_user(org_id, "noid", github_id=None)
    key_model, raw = create_api_key(org_id=org_id, name="g6", scope=APIKeyScope.FULL)

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add(with_id)
        session.add(no_id)
        session.add(key_model)

    try:
        yield org_id, raw, with_id, no_id
    finally:
        await _purge(
            org_ids=[org_id],
            user_ids=[with_id.id, no_id.id],
            api_key_ids=[key_model.id],
        )


@requires_db
@pytest.mark.asyncio
async def test_list_users_surfaces_github_id(org_with_full_key):
    org_id, raw, with_id, no_id = org_with_full_key
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/users", headers={"Authorization": f"Bearer {raw}"}
        )

    assert resp.status_code == 200
    by_id = {u["id"]: u for u in resp.json()}

    assert "github_id" in by_id[with_id.id]
    assert by_id[with_id.id]["github_id"] == "9999001"
    # unset github_id serializes as null, not absent
    assert by_id[no_id.id]["github_id"] is None


def test_user_response_serializes_github_id():
    """Unit-level guard (no DB): UserResponse carries github_id, defaulting None."""
    payload = UserResponse(
        id="user_x",
        email="x@example.com",
        name=None,
        github_username="octocat",
        github_id="583231",
        role="member",
        org_id="org_x",
        created_at="2026-01-01T00:00:00+00:00",
    ).model_dump()
    assert payload["github_id"] == "583231"

    defaulted = UserResponse(
        id="user_y",
        email="y@example.com",
        name=None,
        github_username=None,
        role="member",
        org_id="org_y",
        created_at="2026-01-01T00:00:00+00:00",
    ).model_dump()
    assert defaulted["github_id"] is None

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager

import pytest

from api.routers import cost_excluded_keys
from auth import AuthContext, AuthMethod
from models import UserRole
from oddish.db import generate_id, utcnow


@pytest.mark.asyncio
async def test_add_key_stores_only_hash_and_hint(monkeypatch):
    class Session:
        added = None

        def add(self, row):
            self.added = row

        async def commit(self):
            self.added.id = generate_id()
            self.added.created_at = utcnow()

    session = Session()

    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(cost_excluded_keys, "get_session", get_session)
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "org_1")
    auth = AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_1",
        user_id="admin_1",
        user_role=UserRole.ADMIN,
    )
    raw_key = "xai-secret-provider-key-9f2c"

    response = await cost_excluded_keys.add_cost_excluded_key(
        cost_excluded_keys.CreateCostExcludedKeyRequest(key=raw_key, label="sponsored"),
        auth,
    )

    assert session.added.key_hash == hashlib.sha256(raw_key.encode()).hexdigest()
    assert session.added.key_hint == "9f2c"
    assert raw_key not in response.model_dump_json()

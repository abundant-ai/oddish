from datetime import timedelta
from types import SimpleNamespace

import pytest

from auth.verification import verify_api_key
from models import APIKeyScope, create_api_key
from oddish.db import utcnow


def test_create_api_key_sets_is_internal():
    model, raw = create_api_key(
        org_id="org_1", name="analyzer:s1", scope=APIKeyScope.READ, is_internal=True
    )
    assert model.is_internal is True
    assert raw.startswith("ok_")


def test_create_api_key_defaults_not_internal():
    model, _ = create_api_key(org_id="org_1", name="user key")
    assert model.is_internal is False


def test_create_api_key_stores_creator_role():
    model, _ = create_api_key(
        org_id="org_1",
        name="member tasks",
        scope=APIKeyScope.TASKS,
        created_by_user_id="user_1",
        created_by_role="member",
    )

    assert model.created_by_role == "member"


def test_create_api_key_can_bind_to_analysis_trial_without_copying_resources():
    model, _ = create_api_key(
        org_id="org_1",
        name="analysis:qa-1",
        scope=APIKeyScope.READ,
        is_internal=True,
        bound_analysis_trial_id="qa-1",
    )

    assert model.bound_analysis_trial_id == "qa-1"
    assert not hasattr(model, "allowed_trial_ids")


@pytest.mark.asyncio
async def test_expired_bound_analysis_key_is_rejected_before_resource_access():
    expired_key = SimpleNamespace(
        expires_at=utcnow() - timedelta(seconds=1),
        bound_analysis_trial_id="qa-1",
    )

    class Result:
        def scalar_one_or_none(self):
            return expired_key

    class Session:
        calls = 0

        async def execute(self, _query):
            self.calls += 1
            return Result()

    session = Session()
    assert await verify_api_key(session, "ok_expired_bound_key") is None
    assert session.calls == 1

from datetime import timedelta
from types import SimpleNamespace

import pytest

from auth.verification import verify_api_key
from models import APIKeyModel, APIKeyScope, create_api_key
from oddish.db import utcnow
from oddish.db.models import TrialModel


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
    trial_id = "qa-post-trial-01-apache__kafka-21033-9-run-20260831-102300-12451f9d-2"
    model, _ = create_api_key(
        org_id="org_1",
        name=f"analysis:{trial_id}",
        scope=APIKeyScope.READ,
        is_internal=True,
        bound_analysis_trial_id=trial_id,
    )

    assert len(trial_id) > 64
    assert model.bound_analysis_trial_id == trial_id
    assert not hasattr(model, "allowed_trial_ids")


def test_bound_analysis_trial_id_uses_the_trial_primary_key_length():
    bound_id_type = APIKeyModel.__table__.c.bound_analysis_trial_id.type
    trial_id_type = TrialModel.__table__.c.id.type

    assert bound_id_type.length == trial_id_type.length == 160


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

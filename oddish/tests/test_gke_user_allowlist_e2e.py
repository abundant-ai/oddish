"""End-to-end GKE user-allowlist enforcement through create_task_sweep_core.

DB-backed (like test_create_task_sweep_harbor.py): needs a Postgres. Proves that
a hosted submission whose authenticated user email is not allowlisted is rejected
403 with ZERO trials persisted across all three GKE routing paths (explicit
env=gke, override_tpu, append-inherit) and when the email is None (a creatorless
key), while an allowlisted user is admitted and routes to the gke variant, and
the OSS path (org_id None, unauthenticated) is exempt. The unit-level gate is in
test_gke_user_allowlist_gate.py; this pins the behavior through the real
submission funnel and real persistence.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType
from sqlalchemy import func, select

from oddish.config import QuotaMode, settings
from oddish.core.endpoints import create_task_sweep_core
from oddish.db import TaskModel, TrialModel, get_session
from oddish.schemas import AgentModelPair, HarborConfig, TaskSweepSubmission

pytestmark = pytest.mark.asyncio

ALLOWED_EMAIL = "operator@abundant.ai"
DENIED_EMAIL = "mallory@evil.com"
ORG = "org-hosted-e2e"


@pytest.fixture(autouse=True)
def _quota_off_and_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.OFF)
    monkeypatch.setattr(settings, "gke_allowed_user_emails", ALLOWED_EMAIL)


async def _seed_task(session, tid, org_id):
    await session.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))
    session.add(TaskModel(id=tid, name=tid, user="t", org_id=org_id, task_path="p"))
    await session.flush()


async def _trial_count(session, tid) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(TrialModel).where(TrialModel.task_id == tid)
        )
    ).scalar_one()


def _gke_submission(tid, **harbor_kw):
    return TaskSweepSubmission(
        task_id=tid,
        configs=[AgentModelPair(agent="nop", n_trials=1)],
        harbor=HarborConfig(**harbor_kw),
        environment=EnvironmentType.GKE,
    )


async def test_explicit_gke_non_allowlisted_user_rejected_zero_trials():
    async with get_session() as session:
        await _seed_task(session, "e2e-u-explicit", ORG)
        with pytest.raises(HTTPException) as exc:
            await create_task_sweep_core(
                session,
                submission=_gke_submission("e2e-u-explicit"),
                org_id=ORG,
                user_email=DENIED_EMAIL,
            )
        assert exc.value.status_code == 403
        assert "not enabled" in str(exc.value.detail).lower()
        assert await _trial_count(session, "e2e-u-explicit") == 0


async def test_hosted_none_email_rejected_zero_trials():
    # A creatorless API key -> user_email None -> default-deny on hosted.
    async with get_session() as session:
        await _seed_task(session, "e2e-u-none", ORG)
        with pytest.raises(HTTPException) as exc:
            await create_task_sweep_core(
                session,
                submission=_gke_submission("e2e-u-none"),
                org_id=ORG,
                user_email=None,
            )
        assert exc.value.status_code == 403
        assert await _trial_count(session, "e2e-u-none") == 0


async def test_override_tpu_non_allowlisted_user_rejected_zero_trials():
    async with get_session() as session:
        await _seed_task(session, "e2e-u-tpu", ORG)
        with pytest.raises(HTTPException) as exc:
            await create_task_sweep_core(
                session,
                submission=TaskSweepSubmission(
                    task_id="e2e-u-tpu",
                    configs=[AgentModelPair(agent="nop", n_trials=1)],
                    harbor=HarborConfig(
                        environment={"override_tpu": {"type": "v5e", "topology": "2x2"}}
                    ),
                ),
                org_id=ORG,
                user_email=DENIED_EMAIL,
            )
        assert exc.value.status_code == 403
        assert await _trial_count(session, "e2e-u-tpu") == 0


async def test_append_inherited_gke_non_allowlisted_user_rejected():
    async with get_session() as session:
        await _seed_task(session, "e2e-u-inherit", ORG)
        # Seed a real existing GKE trial via the allowlisted user, then append as
        # a non-allowlisted user in the same org (no --env: inherits GKE).
        await create_task_sweep_core(
            session,
            submission=_gke_submission("e2e-u-inherit"),
            org_id=ORG,
            user_email=ALLOWED_EMAIL,
        )
        before = await _trial_count(session, "e2e-u-inherit")
        with pytest.raises(HTTPException) as exc:
            await create_task_sweep_core(
                session,
                submission=TaskSweepSubmission(
                    task_id="e2e-u-inherit",
                    configs=[AgentModelPair(agent="nop", n_trials=1)],
                    harbor=HarborConfig(),
                ),
                org_id=ORG,
                user_email=DENIED_EMAIL,
            )
        assert exc.value.status_code == 403
        assert await _trial_count(session, "e2e-u-inherit") == before


async def test_oss_unauthenticated_is_exempt():
    async with get_session() as session:
        await _seed_task(session, "e2e-u-oss", None)
        _t, trials, _a, _e = await create_task_sweep_core(
            session,
            submission=_gke_submission("e2e-u-oss"),
            org_id=None,
            user_email=None,
        )
        assert trials


async def test_allowlisted_user_admitted_and_routes_to_gke():
    async with get_session() as session:
        await _seed_task(session, "e2e-u-allowed", ORG)
        # Case-insensitive: uppercased email still matches the lowercased list.
        _t, trials, _a, _e = await create_task_sweep_core(
            session,
            submission=_gke_submission("e2e-u-allowed"),
            org_id=ORG,
            user_email=ALLOWED_EMAIL.upper(),
        )
        assert trials
        assert {(t.environment or "") for t in trials} == {EnvironmentType.GKE.value}

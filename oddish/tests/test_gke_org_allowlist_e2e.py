"""End-to-end GKE org-allowlist enforcement through create_task_sweep_core.

DB-backed (like test_create_task_sweep_harbor.py): needs a Postgres. Proves that
a non-allowlisted org is rejected 403 with ZERO trials persisted across all three
GKE routing paths (explicit env=gke, override_tpu, append-inherit), while an
allowlisted org is admitted and routes to the gke variant, and the OSS path
(org_id=None) is exempt. The unit-level gate is covered in
test_gke_org_allowlist_gate.py; this pins the behavior through the real
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

ALLOWED = "org-allowed-e2e"
DENIED = "org-denied-e2e"


@pytest.fixture(autouse=True)
def _quota_off_and_allowlist(monkeypatch):
    # Isolate the GKE gate from quota admission; allowlist ALLOWED only.
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.OFF)
    monkeypatch.setattr(settings, "gke_allowed_org_ids", ALLOWED)


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


async def test_explicit_gke_non_allowlisted_org_rejected_zero_trials():
    async with get_session() as session:
        await _seed_task(session, "e2e-explicit", DENIED)
        with pytest.raises(HTTPException) as exc:
            await create_task_sweep_core(
                session, submission=_gke_submission("e2e-explicit"), org_id=DENIED
            )
        assert exc.value.status_code == 403
        assert "not enabled" in str(exc.value.detail).lower()
        assert await _trial_count(session, "e2e-explicit") == 0


async def test_override_tpu_non_allowlisted_org_rejected_zero_trials():
    async with get_session() as session:
        await _seed_task(session, "e2e-tpu", DENIED)
        with pytest.raises(HTTPException) as exc:
            await create_task_sweep_core(
                session,
                submission=TaskSweepSubmission(
                    task_id="e2e-tpu",
                    configs=[AgentModelPair(agent="nop", n_trials=1)],
                    harbor=HarborConfig(
                        environment={"override_tpu": {"type": "v5e", "topology": "2x2"}}
                    ),
                ),
                org_id=DENIED,
            )
        assert exc.value.status_code == 403
        assert await _trial_count(session, "e2e-tpu") == 0


async def test_append_inherited_gke_non_allowlisted_org_rejected(monkeypatch):
    async with get_session() as session:
        await _seed_task(session, "e2e-inherit", DENIED)
        # Seed a real existing GKE trial via the normal path, then revoke.
        monkeypatch.setattr(settings, "gke_allowed_org_ids", DENIED)
        await create_task_sweep_core(
            session, submission=_gke_submission("e2e-inherit"), org_id=DENIED
        )
        monkeypatch.setattr(settings, "gke_allowed_org_ids", ALLOWED)
        before = await _trial_count(session, "e2e-inherit")
        with pytest.raises(HTTPException) as exc:
            await create_task_sweep_core(
                session,
                submission=TaskSweepSubmission(
                    task_id="e2e-inherit",
                    configs=[AgentModelPair(agent="nop", n_trials=1)],
                    harbor=HarborConfig(),  # no --env: inherits GKE
                ),
                org_id=DENIED,
            )
        assert exc.value.status_code == 403
        assert await _trial_count(session, "e2e-inherit") == before


async def test_oss_none_org_is_exempt(monkeypatch):
    # org_id=None (OSS single-tenant server) is exempt regardless of the
    # allowlist -- even the deny-all empty allowlist.
    monkeypatch.setattr(settings, "gke_allowed_org_ids", "")
    async with get_session() as session:
        await _seed_task(session, "e2e-oss", None)
        _t, trials, _a, _e = await create_task_sweep_core(
            session, submission=_gke_submission("e2e-oss"), org_id=None
        )
        assert trials


async def test_allowlisted_org_admitted_and_routes_to_gke():
    async with get_session() as session:
        await _seed_task(session, "e2e-allowed", ALLOWED)
        _t, trials, _a, _e = await create_task_sweep_core(
            session, submission=_gke_submission("e2e-allowed"), org_id=ALLOWED
        )
        assert trials
        assert {(t.environment or "") for t in trials} == {EnvironmentType.GKE.value}

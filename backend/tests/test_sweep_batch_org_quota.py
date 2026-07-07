"""Batch sweep vs the org-level aggregate monthly cap, end to end (real Postgres).

Under ENFORCE with a low org cap, a multi-item batch where earlier items fit
but a later item tips the org over must fail ONLY that item with the 402
org-quota error, keep sibling successes persisted, and leave the session usable
for the items that follow (savepoint isolation). Note the org admission check
runs BEFORE any trial insert, so a quota-failed item writes nothing by design;
the rollback/isolation contract is observable through the org math itself: the
item after the failed one admits against only the persisted in-flight trials.

The route-level test proves the 207 Multi-Status semantics of
``POST /tasks/sweep/batch`` on a mixed success/402 outcome.

Runs against ``ODDISH_DATABASE_URL`` (schema is dropped and rebuilt via
``create_all`` like tests/test_sweep_batch.py); skips otherwise.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (  # type: ignore[attr-defined]
    async_sessionmaker,
    create_async_engine,
)

import models  # noqa: F401  registers cloud tables (incl. org_quotas) on the shared Base
from api.app import create_app
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import UserModel, UserRole
from oddish.config import QuotaMode, settings
from oddish.core.endpoints import create_task_sweep_batch_core
from oddish.db import TrialModel
from oddish.db.models import Base
from oddish.schemas import AgentModelPair, TaskSweepSubmission

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]

ORG_ID = "org-batch-oq"
BILLED_USER = "user-batch-oq"


@pytest.fixture
def _enforced_org_cap(monkeypatch):
    """Low org cap, generous per-user cap: only the ORG gate can bite."""
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.20"))
    monkeypatch.setattr(settings, "default_daily_quota_usd", Decimal("1000"))
    monkeypatch.setattr(settings, "default_org_monthly_quota_usd", Decimal("0.50"))


async def _reset_schema_and_seed(engine, task_ids: list[str]) -> None:
    """Drop/recreate the public schema and seed one org + append-target tasks.

    Mirrors tests/test_sweep_batch.py, including the migration-only partial
    unique index the sweep's TAG_PROJECT enqueue upserts against.
    """
    async with engine.begin() as c:
        await c.execute(text("drop schema public cascade"))
        await c.execute(text("create schema public"))
        await c.run_sync(Base.metadata.create_all)
        await c.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_jobs_tag_project_active "
                "ON worker_jobs (kind, subject_table, subject_id) "
                "WHERE kind = 'TAG_PROJECT' "
                "AND status IN ('QUEUED', 'RETRYING') "
                "AND subject_id IS NOT NULL"
            )
        )
        await c.execute(
            text(
                "insert into organizations "
                "(id,name,slug,plan,settings,is_active,created_at,updated_at) "
                "values (:org,'BatchOQ','batch-oq','free','{}'::jsonb,true,now(),now())"
            ),
            {"org": ORG_ID},
        )
        for task_id in task_ids:
            await c.execute(
                text(
                    "insert into tasks "
                    '(id,name,org_id,"user",priority,status,task_path,tags,'
                    "run_analysis,run_probe,created_at,updated_at) "
                    "values (:id,:id,:org,'u','LOW','COMPLETED','p',"
                    "'{}'::jsonb,false,false,now(),now())"
                ),
                {"id": task_id, "org": ORG_ID},
            )


def _append_submission(task_id: str, n_trials: int) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id=task_id,
        append_to_task=True,
        configs=[
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=n_trials,
            )
        ],
        user="alice",
    )


async def _trial_count(maker, task_id: str) -> int:
    async with maker() as session:
        return await session.scalar(
            select(func.count())
            .select_from(TrialModel)
            .where(TrialModel.task_id == task_id)
        )


# --- core level: the tipping item 402s, siblings persist, session survives ------


async def test_batch_item_tipping_org_cap_fails_402_and_siblings_persist(
    _enforced_org_cap,
):
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _reset_schema_and_seed(engine, ["t-oq-a", "t-oq-b", "t-oq-c"])

        submissions = [
            # 1 trial -> org reserved 0.20 < 0.50: admitted.
            _append_submission("t-oq-a", n_trials=1),
            # 3 trials -> 0.20 in-flight + 0.60 = 0.80 >= 0.50: org 402.
            _append_submission("t-oq-b", n_trials=3),
            # 1 trial -> 0.20 + 0.20 = 0.40 < 0.50: admitted, but ONLY if the
            # failed item's savepoint rolled back cleanly (a leaked item-1
            # reservation would push this to 1.00 and 402 it too).
            _append_submission("t-oq-c", n_trials=1),
        ]

        async def resolve(session, submission):
            return BILLED_USER

        async with maker() as session:
            results = await create_task_sweep_batch_core(
                session,
                submissions=submissions,
                org_id=ORG_ID,
                resolve_billed_user_id=resolve,
            )
            await session.commit()

        # (a) the over-budget item gets the 402 org-quota failure, per-item.
        assert results[1].success is False
        assert results[1].status_code == 402
        assert "organization is over its monthly budget" in (results[1].error or "")

        # (d-core) mixed outcomes: siblings succeeded around the failure.
        assert results[0].success is True
        assert results[0].status_code == 200
        assert results[2].success is True
        assert results[2].status_code == 200

        # (b) sibling successes persisted; (c) the failed item wrote nothing.
        assert await _trial_count(maker, "t-oq-a") == 1
        assert await _trial_count(maker, "t-oq-b") == 0
        assert await _trial_count(maker, "t-oq-c") == 1
    finally:
        await engine.dispose()


# --- route level: mixed results -> HTTP 207 Multi-Status -------------------------


async def test_batch_route_returns_207_on_mixed_org_quota_results(_enforced_org_cap):
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    user_id = f"user_oqr_{uuid.uuid4().hex[:8]}"
    try:
        await _reset_schema_and_seed(engine, ["t-oqr-a", "t-oqr-b"])
        async with maker() as session:
            session.add(
                UserModel(
                    id=user_id,
                    org_id=ORG_ID,
                    email=f"{user_id}@example.com",
                    role=UserRole.ADMIN,
                    is_active=True,
                )
            )
            await session.commit()

        app = create_app()
        app.dependency_overrides[require_auth] = lambda: AuthContext(
            method=AuthMethod.CLERK_JWT,
            org_id=ORG_ID,
            user_id=user_id,
            user_role=UserRole.ADMIN,
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/tasks/sweep/batch",
                    json={
                        "submissions": [
                            _append_submission("t-oqr-a", n_trials=1).model_dump(
                                mode="json"
                            ),
                            _append_submission("t-oqr-b", n_trials=3).model_dump(
                                mode="json"
                            ),
                        ]
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 207
        body = response.json()
        assert body["total"] == 2
        assert body["succeeded"] == 1
        assert body["failed"] == 1
        assert body["results"][0]["success"] is True
        assert body["results"][1]["success"] is False
        assert body["results"][1]["status_code"] == 402
        assert "organization is over its monthly budget" in (
            body["results"][1]["error"] or ""
        )

        assert await _trial_count(maker, "t-oqr-a") == 1
        assert await _trial_count(maker, "t-oqr-b") == 0
    finally:
        await engine.dispose()

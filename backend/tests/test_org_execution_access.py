"""Approval checks use real Postgres; no paid workers or Clerk accounts are created."""

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException

import auth
import auth.provisioning as provisioning
import worker.org_access as worker_access
from api.routers import api_keys, tasks, trials, qa_eval
from auth.types import AuthContext, AuthMethod
from models import OrganizationModel, UserModel, UserRole
from oddish.db import get_session
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.queue.worker_job_single_job import (
    JobAccessDenied,
    run_authorized_handler,
)
from org_access import require_execution_org

pytestmark = pytest.mark.skipif(
    not os.environ.get("ODDISH_DATABASE_URL"), reason="local PostgreSQL required"
)


@pytest_asyncio.fixture
async def org_id():
    value = f"approval_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            OrganizationModel(id=value, name="Abundant", slug=value, clerk_org_id=value)
        )
    yield value
    async with get_session() as session:
        await session.execute(
            UserModel.__table__.delete().where(UserModel.org_id == value)
        )
        await session.execute(
            OrganizationModel.__table__.delete().where(OrganizationModel.id == value)
        )


async def set_approval(org_id, enabled):
    async with get_session() as session:
        await session.execute(
            OrganizationModel.__table__.update()
            .where(OrganizationModel.id == org_id)
            .values(execution_enabled=enabled)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [AuthMethod.CLERK_JWT, AuthMethod.API_KEY])
async def test_cached_identities_do_not_bypass_approval_or_revocation(org_id, method):
    context = AuthContext(method=method, org_id=org_id, user_role=UserRole.ADMIN)
    with pytest.raises(HTTPException, match="403"):
        await auth.require_auth(SimpleNamespace(), context)
    await set_approval(org_id, True)
    assert await auth.require_auth(SimpleNamespace(), context) is context
    await set_approval(org_id, False)
    with pytest.raises(HTTPException, match="403"):
        await auth.require_auth(SimpleNamespace(), context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api-keys",
        "/tasks/sweep",
        "/tasks/sweep/batch",
        "/trials/t/retry",
        "/tasks/t/qa/retry",
        "/tasks/t/qa/backfill",
        "/tasks/t/qa/pre-trial",
        "/trials/t/analysis/rerun",
        "/trials/t/trajectory/summary",
        "/qa-evals",
    ],
)
async def test_every_paid_route_rejects_unapproved_admin_before_handler(org_id, path):
    app = FastAPI()
    for router in (api_keys.router, tasks.router, trials.router, qa_eval.router):
        app.include_router(router)
    app.dependency_overrides[auth.get_auth_context] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=org_id,
        user_role=UserRole.ADMIN,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(path, json={})
    assert response.status_code == 403, response.text
    assert "Abundant approval" in response.text


@pytest.mark.asyncio
async def test_missing_org_and_database_failure_never_allow_execution(monkeypatch):
    with pytest.raises(HTTPException):
        await require_execution_org(None)
    import org_access

    monkeypatch.setattr(
        org_access,
        "get_read_session",
        lambda: (_ for _ in ()).throw(ConnectionError("database unavailable")),
    )
    with pytest.raises(ConnectionError):
        await require_execution_org("some-org")


@pytest.mark.asyncio
async def test_worker_blocks_before_handler_and_cancels_after_revocation(org_id):
    job = SimpleNamespace(org_id=org_id)
    handler = SimpleNamespace(run=AsyncMock(return_value=JobOutcome.ok()))
    with pytest.raises(JobAccessDenied):
        await run_authorized_handler(job, handler, worker_access.authorize_worker_job)
    handler.run.assert_not_awaited()
    await set_approval(org_id, True)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def paid_work(_job):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    handler.run = paid_work
    execution = asyncio.create_task(
        run_authorized_handler(
            job, handler, worker_access.authorize_worker_job, poll_seconds=0.01
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    await set_approval(org_id, False)
    with pytest.raises(JobAccessDenied):
        await asyncio.wait_for(execution, 2)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_dispatcher_excludes_unapproved_and_ownerless_jobs(org_id, monkeypatch):
    def key(org):
        return (org, "model", "default", "default", False)

    counts = {key(org_id): 3, key("foreign"): 4, key(None): 5}
    monkeypatch.setattr(
        worker_access,
        "get_worker_job_org_queue_counts",
        AsyncMock(return_value=(counts, {})),
    )
    assert await worker_access.approved_worker_job_counts(["model"]) == ({}, {})
    await set_approval(org_id, True)
    assert await worker_access.approved_worker_job_counts(["model"]) == (
        {key(org_id): 3},
        {},
    )


@pytest.mark.asyncio
async def test_first_request_syncs_before_webhook_without_approving(monkeypatch):
    clerk_id = f"org_sync_{uuid.uuid4().hex[:8]}"
    client = AsyncMock()
    client.get.return_value = httpx.Response(
        200,
        json={"name": "New org", "slug": clerk_id},
        request=httpx.Request("GET", "https://api.clerk.com"),
    )
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    monkeypatch.setattr(provisioning, "CLERK_SECRET_KEY", "test-only")
    monkeypatch.setattr(provisioning, "RequestTimedAsyncClient", lambda **kwargs: cm)
    monkeypatch.setattr(provisioning, "_refresh_user_github_identity", AsyncMock())
    try:
        async with get_session() as session:
            user, org = await provisioning.get_or_create_user_from_clerk(
                session, "user_new", clerk_id, "new@example.test", "org:admin"
            )
            org_id = org.id
            assert org.execution_enabled is False
        async with get_session() as session:
            same = await provisioning.sync_clerk_org(
                session, clerk_id, "New org", clerk_id
            )
            assert same.id == org_id and same.execution_enabled is False
        with pytest.raises(HTTPException):
            await require_execution_org(org_id)
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete().where(
                    UserModel.clerk_user_id == "user_new"
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.clerk_org_id == clerk_id
                )
            )


@pytest.mark.asyncio
async def test_concurrent_org_callbacks_reuse_one_record_and_preserve_revocation(
    org_id,
):
    async def callback():
        async with get_session() as session:
            return (
                await provisioning.sync_clerk_org(
                    session, org_id + "_new", "Same name", "same-name"
                )
            ).id

    try:
        first, second = await asyncio.gather(callback(), callback())
        assert first == second
        await set_approval(first, True)
        async with get_session() as session:
            await session.execute(
                OrganizationModel.__table__.update()
                .where(OrganizationModel.id == first)
                .values(is_active=False, execution_enabled=False)
            )
        assert await callback() == first
        with pytest.raises(HTTPException):
            await require_execution_org(first)
    finally:
        async with get_session() as session:
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.clerk_org_id == org_id + "_new"
                )
            )


@pytest.mark.asyncio
async def test_clerk_outage_returns_retryable_error_without_creating_personal(
    monkeypatch,
):
    monkeypatch.setattr(provisioning, "CLERK_SECRET_KEY", "test-only")
    cm = AsyncMock()
    cm.__aenter__.return_value.get.side_effect = httpx.ConnectError("Clerk unavailable")
    monkeypatch.setattr(provisioning, "RequestTimedAsyncClient", lambda **kwargs: cm)
    session = AsyncMock()
    with pytest.raises(HTTPException) as rejected:
        await provisioning.fetch_and_sync_clerk_org(session, "org_missing")
    assert rejected.value.status_code == 503
    assert session.mock_calls == []


@pytest.mark.asyncio
async def test_operator_approval_requires_budget_and_revoke_denies(org_id, monkeypatch):
    from decimal import Decimal
    import provision_org
    from models import OrgQuotaModel
    from sqlalchemy import select

    async def fetch(session, clerk_id):
        return await session.get(OrganizationModel, clerk_id)

    monkeypatch.setattr(provision_org, "fetch_and_sync_clerk_org", fetch)
    try:
        for limit in (
            None,
            Decimal("0"),
            Decimal("-1"),
            Decimal("NaN"),
            Decimal("Infinity"),
        ):
            with pytest.raises(ValueError):
                await provision_org.provision(org_id, enable=True, monthly_limit=limit)
            with pytest.raises(HTTPException):
                await require_execution_org(org_id)
        await provision_org.provision(org_id, enable=True, monthly_limit=Decimal("50"))
        await require_execution_org(org_id)
        await provision_org.provision(org_id, enable=True, monthly_limit=Decimal("75"))
        async with get_session() as session:
            quota = await session.scalar(
                select(OrgQuotaModel).where(OrgQuotaModel.org_id == org_id)
            )
            assert quota.limit_usd == Decimal("75")
        await provision_org.provision(org_id, enable=False, monthly_limit=None)
        with pytest.raises(HTTPException):
            await require_execution_org(org_id)
    finally:
        async with get_session() as session:
            await session.execute(
                OrgQuotaModel.__table__.delete().where(OrgQuotaModel.org_id == org_id)
            )


@pytest.mark.asyncio
async def test_reconciler_cancels_task_level_jobs_without_trials(org_id, monkeypatch):
    from oddish.db import TaskModel, WorkerJobModel, WorkerJobKind, WorkerJobStatus

    task_id = f"task_{uuid.uuid4().hex[:8]}"
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    teardown = AsyncMock()
    monkeypatch.setattr(worker_access, "terminate_run_harvest", teardown)
    try:
        async with get_session() as session:
            session.add(
                TaskModel(
                    id=task_id,
                    org_id=org_id,
                    name=task_id,
                    user="test",
                    task_path="test",
                )
            )
            session.add(
                WorkerJobModel(
                    id=job_id,
                    org_id=org_id,
                    kind=WorkerJobKind.VERDICT,
                    subject_table="tasks",
                    subject_id=task_id,
                    queue_key="default",
                    modal_function_call_id="fake-remote-call",
                )
            )
        await set_approval(org_id, True)
        assert await worker_access.cancel_unapproved_runs() == 0
        await set_approval(org_id, False)
        assert await worker_access.cancel_unapproved_runs() == 1
        assert (
            "fake-remote-call" in teardown.call_args.args[0]["modal_function_call_ids"]
        )
        async with get_session() as session:
            assert (
                await session.get(WorkerJobModel, job_id)
            ).status == WorkerJobStatus.CANCELLED
        assert await worker_access.cancel_unapproved_runs() == 0
    finally:
        async with get_session() as session:
            await session.execute(
                WorkerJobModel.__table__.delete().where(WorkerJobModel.id == job_id)
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )


@pytest.mark.asyncio
async def test_deleted_org_callback_revokes_and_stale_creation_cannot_restore(
    org_id, monkeypatch
):
    from api.routers import clerk_webhooks

    await set_approval(org_id, True)
    app = FastAPI()
    app.include_router(clerk_webhooks.router)
    monkeypatch.setattr(
        clerk_webhooks,
        "_verify_clerk_webhook",
        lambda *_: {"type": "organization.deleted", "data": {"id": org_id}},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/webhooks/clerk", json={})).status_code == 200
    async with get_session() as session:
        org = await provisioning.sync_clerk_org(session, org_id, "Stale", "stale")
        assert org.deleted_at is not None and org.execution_enabled is False
    with pytest.raises(HTTPException):
        await require_execution_org(org_id)


@pytest.mark.asyncio
async def test_signed_v2_token_resolves_selected_org(monkeypatch):
    import time
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwt, jwk
    import auth.verification as verification

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwk.construct(key.public_key(), "RS256").to_dict()
    public["kid"] = "test-key"
    monkeypatch.setattr(
        verification, "get_clerk_jwks", AsyncMock(return_value={"keys": [public]})
    )
    monkeypatch.setattr(verification, "CLERK_ISSUER", "https://clerk.test")
    monkeypatch.setattr(verification, "CLERK_JWT_AUDIENCE", "")
    token = jwt.encode(
        {
            "iss": "https://clerk.test",
            "sub": "user_test",
            "exp": int(time.time()) + 60,
            "v": 2,
            "o": {"id": "org_selected", "rol": "admin"},
        },
        key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    claims = await verification.verify_clerk_jwt(token)
    assert claims["org_id"] == "org_selected" and claims["org_role"] == "admin"


@pytest.mark.asyncio
async def test_migration_approves_only_reviewed_active_clerk_ids():
    import importlib.util
    from datetime import datetime, timezone
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    spec = importlib.util.spec_from_file_location(
        "org_execution_migration",
        Path(__file__).parents[1] / "alembic/versions/org_execution_001.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def exercise(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            fixtures = [
                ("reviewed-main", "org_39ufkEqie8rLlVhoK4YMm4IMx0L", True, None),
                ("reviewed-cyber", "org_3H67wVrUZObfjW9JnxGq5pUZQvN", False, None),
                (
                    "reviewed-onsite",
                    "org_3IVmVHXFyfF4ltX8bfQMQTHrH1y",
                    True,
                    datetime(2026, 9, 1, tzinfo=timezone.utc),
                ),
                ("unreviewed-name", "org_someone_else", True, None),
            ]
            for row_id, clerk_id, active, deleted in fixtures:
                connection.execute(
                    text(
                        "INSERT INTO organizations (id, name, slug, clerk_org_id, is_active, deleted_at, plan, settings, created_at, updated_at) VALUES (:id, 'Abundant', :id, :clerk, :active, CAST(:deleted AS timestamptz), 'free', '{}'::jsonb, NOW(), NOW())"
                    ),
                    {
                        "id": row_id,
                        "clerk": clerk_id,
                        "active": active,
                        "deleted": deleted,
                    },
                )
            migration.upgrade()
            results = dict(
                connection.execute(
                    text(
                        "SELECT id, execution_enabled FROM organizations WHERE id IN ('reviewed-main', 'reviewed-cyber', 'reviewed-onsite', 'unreviewed-name')"
                    )
                ).all()
            )
            assert results == {
                "reviewed-main": True,
                "reviewed-cyber": False,
                "reviewed-onsite": False,
                "unreviewed-name": False,
            }
            connection.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, plan, is_active, settings, created_at, updated_at) VALUES ('default-denied', 'New', 'default-denied', 'free', true, '{}'::jsonb, NOW(), NOW())"
                )
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT execution_enabled FROM organizations WHERE id = 'default-denied'"
                    )
                )
                is False
            )

    async with get_session() as session:
        transaction = await session.begin_nested()
        try:
            await (await session.connection()).run_sync(exercise)
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_clerk_membership_payload_creates_then_deletes_the_member(
    org_id, monkeypatch
):
    from api.routers import clerk_webhooks
    from sqlalchemy import select

    monkeypatch.setattr(provisioning, "_refresh_user_github_identity", AsyncMock())
    event = {
        "type": "organizationMembership.created",
        "data": {
            "id": "orgmem_test",
            "organization": {"id": org_id},
            "role": "org:admin",
            "public_user_data": {
                "user_id": "user_nested",
                "identifier": "nested@example.test",
            },
        },
    }
    monkeypatch.setattr(clerk_webhooks, "_verify_clerk_webhook", lambda *_: event)
    app = FastAPI()
    app.include_router(clerk_webhooks.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/webhooks/clerk", json={})).status_code == 200
        async with get_session() as session:
            user = await session.scalar(
                select(UserModel).where(UserModel.org_id == org_id)
            )
            assert user.clerk_user_id == "user_nested" and user.role == UserRole.ADMIN
        with pytest.raises(HTTPException):
            await require_execution_org(org_id)
        event["type"] = "organizationMembership.deleted"
        assert (await client.post("/webhooks/clerk", json={})).status_code == 200
        async with get_session() as session:
            assert (
                await session.scalar(
                    select(UserModel).where(UserModel.org_id == org_id)
                )
                is None
            )

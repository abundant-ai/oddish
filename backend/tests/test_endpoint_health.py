"""Real PostgreSQL ownership/atomicity checks; provider requests are mocked."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import endpoint_health as health
from endpoint_health import CheckResult, EndpointTarget
from oddish.db import utcnow

TARGET = EndpointTarget(
    name="Primary", model="openai/test", api_key_env="TEST_PROVIDER_KEY"
)


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("ENDPOINT_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set ENDPOINT_TEST_DATABASE_URL to a disposable PostgreSQL database"
        )
    engine = create_async_engine(url)
    path = Path(__file__).parents[1] / "alembic/versions/endpoint_health_001.py"
    spec = importlib.util.spec_from_file_location("endpoint_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DROP TABLE IF EXISTS endpoint_checks, endpoint_monitors, slack_expense_alerts CASCADE"
            )
        )
        await connection.run_sync(upgrade)
        await connection.execute(
            text(
                "CREATE TABLE slack_expense_alerts (alert_key text PRIMARY KEY, payload text, claimed_at timestamptz, notified_at timestamptz)"
            )
        )
    statements = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, params, context, many: statements.append(
            statement
        ),
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with maker.begin() as transaction:
            yield transaction

    yield SimpleNamespace(
        engine=engine, session=session, statements=statements, migration=migration
    )
    await engine.dispose()


async def claim(db, now=None, targets=None):
    async with db.session() as session:
        if targets is not None:
            await health.sync_targets(session, targets)
        return await health.claim_checks(
            session,
            now or utcnow(),
            [t.id for t in targets] if targets is not None else [TARGET.id],
        )


async def save(db, claimed, outcome, when=None):
    results = [
        CheckResult(
            row["id"],
            outcome,
            when or utcnow(),
            20,
            None if outcome == "success" else "Example failure",
        )
        for row in claimed
    ]
    async with db.session() as session:
        return await health.save_results(session, claimed, results, alerts_enabled=True)


async def rows(db, table):
    assert table in {"endpoint_monitors", "endpoint_checks", "slack_expense_alerts"}
    async with db.session() as session:
        return (await session.execute(text(f"SELECT * FROM {table}"))).mappings().all()


@pytest.mark.asyncio
async def test_failure_confirmation_one_incident_and_recovery(db):
    now = utcnow() + timedelta(seconds=1)
    await save(db, await claim(db, now, [TARGET]), "failure", now)
    assert not await rows(db, "slack_expense_alerts")
    assert not await claim(db, now + timedelta(seconds=30))
    second = await claim(db, now + health.CONFIRM_INTERVAL)
    db.statements.clear()
    await save(db, second, "failure", now + health.CONFIRM_INTERVAL)
    assert len(db.statements) == 3
    incident = (await rows(db, "endpoint_monitors"))[0]["incident_id"]
    assert incident
    await save(
        db,
        await claim(db, now + health.CONFIRM_INTERVAL * 2),
        "failure",
        now + health.CONFIRM_INTERVAL * 2,
    )
    assert len(await rows(db, "slack_expense_alerts")) == 1
    await save(
        db,
        await claim(db, now + health.CONFIRM_INTERVAL * 3),
        "success",
        now + health.CONFIRM_INTERVAL * 3,
    )
    assert (await rows(db, "endpoint_monitors"))[0]["incident_id"] is None
    alerts = await rows(db, "slack_expense_alerts")
    assert {a["alert_key"] for a in alerts} == {
        f"endpoint:{incident}:opened",
        f"endpoint:{incident}:recovered",
    }
    assert len(await rows(db, "endpoint_checks")) == 4
    assert all(a["notified_at"] is None for a in alerts)


@pytest.mark.asyncio
async def test_concurrent_claims_skip_locked_rows(db):
    async with db.session() as session:
        await health.sync_targets(session, [TARGET])
    now = utcnow() + timedelta(seconds=1)
    async with db.session() as first_session:
        first = await health.claim_checks(first_session, now, [TARGET.id])
        async with db.session() as second_session:
            second = await asyncio.wait_for(
                health.claim_checks(second_session, now, [TARGET.id]), timeout=2
            )
    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_expired_worker_and_duplicate_completion_cannot_overwrite(db):
    now = utcnow() + timedelta(seconds=1)
    old = await claim(db, now, [TARGET])
    new = await claim(db, now + health.LEASE_DURATION)
    assert new[0]["lease_token"] != old[0]["lease_token"]
    assert await save(db, new, "success") == 1
    assert await save(db, old, "failure") == 0
    assert await save(db, new, "success") == 0
    assert len(await rows(db, "endpoint_checks")) == 1
    assert (await rows(db, "endpoint_monitors"))[0]["last_outcome"] == "success"


@pytest.mark.asyncio
async def test_crash_rolls_back_state_history_and_alert(db):
    now = utcnow() + timedelta(seconds=1)
    await save(db, await claim(db, now, [TARGET]), "failure", now)
    claimed = await claim(db, now + health.CONFIRM_INTERVAL)
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "ALTER TABLE slack_expense_alerts ADD CONSTRAINT simulate_failed_insert CHECK(false)"
            )
        )
    with pytest.raises(Exception, match="simulate_failed_insert"):
        await save(db, claimed, "failure", now + health.CONFIRM_INTERVAL)
    assert (await rows(db, "endpoint_monitors"))[0]["consecutive_failures"] == 1
    assert len(await rows(db, "endpoint_checks")) == 1
    async with db.engine.begin() as conn:
        await conn.execute(
            text(
                "ALTER TABLE slack_expense_alerts DROP CONSTRAINT simulate_failed_insert"
            )
        )
    assert await save(db, claimed, "failure", now + health.CONFIRM_INTERVAL) == 1
    assert len(await rows(db, "slack_expense_alerts")) == 1


@pytest.mark.asyncio
async def test_monitor_defect_does_not_establish_provider_outage(db):
    now = utcnow() + timedelta(seconds=1)
    await save(db, await claim(db, now, [TARGET]), "failure", now)
    await save(
        db,
        await claim(db, now + health.CONFIRM_INTERVAL),
        "monitor_error",
        now + health.CONFIRM_INTERVAL,
    )
    await save(
        db,
        await claim(db, now + health.CONFIRM_INTERVAL * 2),
        "failure",
        now + health.CONFIRM_INTERVAL * 2,
    )
    assert not await rows(db, "slack_expense_alerts")
    assert (await rows(db, "endpoint_monitors"))[0]["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_removed_target_fences_inflight_result(db):
    claimed = await claim(db, utcnow() + timedelta(seconds=1), [TARGET])
    async with db.session() as session:
        await health.sync_targets(session, [])
    assert not await claim(db)
    assert await save(db, claimed, "failure") == 0
    assert not (await rows(db, "endpoint_monitors"))[0]["enabled"]


@pytest.mark.asyncio
async def test_batch_queries_and_no_connection_during_provider_call(db, monkeypatch):
    targets = [
        EndpointTarget(
            name=str(i), model=f"openai/test-{i}", api_key_env="TEST_PROVIDER_KEY"
        )
        for i in range(health.BATCH_SIZE)
    ]
    monkeypatch.setattr(health, "configured_targets", lambda: targets)
    monkeypatch.setattr(health, "get_session", db.session)
    calls, peak = 0, 0

    async def probe(target):
        nonlocal calls, peak
        assert db.engine.pool.checkedout() == 0
        calls += 1
        peak = max(peak, calls)
        await asyncio.sleep(0.01)
        calls -= 1
        return CheckResult(target.id, "success", utcnow(), 10)

    monkeypatch.setattr(health, "check_endpoint", probe)
    db.statements.clear()
    assert await health.run_checks() == health.BATCH_SIZE
    assert peak == health.CONCURRENCY
    assert len(db.statements) == 4  # config sync + claim + states + history
    db.statements.clear()
    assert await health.run_checks() == 0
    assert len(db.statements) == 2


@pytest.mark.asyncio
async def test_operator_api_reads_once_and_check_request_only_schedules(
    db, monkeypatch
):
    from api.routers import admin
    from auth import require_admin

    await save(
        db,
        await claim(db, utcnow() + timedelta(seconds=1), [TARGET]),
        "success",
        utcnow() - timedelta(hours=1),
    )
    monkeypatch.setattr(admin, "get_read_session", db.session)
    monkeypatch.setattr(admin, "get_session", db.session)
    monkeypatch.setenv("ODDISH_OPERATOR_ORG_ID", "operators")
    monkeypatch.setenv("ODDISH_ENABLE_ENDPOINT_MONITORING", "true")
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        org_id="operators"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        db.statements.clear()
        response = await client.get("/admin/endpoint-health")
        assert response.status_code == 200
        assert len(db.statements) == 1
        monitor = response.json()["monitors"][0]
        assert monitor["stale"] is True
        assert "lease_token" not in monitor
        db.statements.clear()
        response = await client.get(f"/admin/endpoint-health/{TARGET.id}/checks")
        assert response.status_code == 200
        assert len(db.statements) == 1
        db.statements.clear()
        response = await client.post(f"/admin/endpoint-health/{TARGET.id}/check")
        assert response.status_code == 202
        assert len(db.statements) == 1
        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            org_id="customer"
        )
        db.statements.clear()
        for method, path in [
            ("GET", ""),
            ("GET", f"/{TARGET.id}/checks"),
            ("POST", f"/{TARGET.id}/check"),
        ]:
            assert (
                await client.request(method, f"/admin/endpoint-health{path}")
            ).status_code == 403
        assert db.statements == []


def test_explicit_connection_identity_and_configuration(monkeypatch):
    assert TARGET.id == TARGET.model_copy(update={"name": "Renamed"}).id
    assert TARGET.id != TARGET.model_copy(update={"api_key_env": "OTHER_KEY"}).id
    assert TARGET.id != TARGET.model_copy(update={"model": "anthropic/test"}).id
    monkeypatch.setenv(
        "ODDISH_ENDPOINT_MONITORS",
        json.dumps([TARGET.model_dump(), TARGET.model_dump()]),
    )
    with pytest.raises(ValueError, match="distinct"):
        health.configured_targets()
    with pytest.raises(ValueError, match="api_key_env"):
        EndpointTarget(name="missing", model="openai/test")


@pytest.mark.asyncio
async def test_probe_uses_named_credential_without_retries_or_response_storage(
    monkeypatch,
):
    import litellm

    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-sentinel")
    call = AsyncMock(
        return_value=SimpleNamespace(
            id="request-1",
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
        )
    )
    monkeypatch.setattr(litellm, "acompletion", call)
    result = await health.check_endpoint(TARGET)
    assert result.outcome == "success"
    assert result.request_id == "request-1"
    assert call.call_args.kwargs["api_key"] == "secret-sentinel"
    assert call.call_args.kwargs["num_retries"] == 0
    assert call.call_args.kwargs["caching"] is False
    assert "secret-sentinel" not in repr(result)
    assert "hello" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,message",
    [
        ("AuthenticationError", "credential"),
        ("RateLimitError", "allowance"),
        ("ServiceUnavailableError", "server error"),
        ("Timeout", "timed out"),
    ],
)
async def test_provider_failures_are_classified_without_echoing_secrets(
    monkeypatch, kind, message
):
    import litellm

    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-sentinel")
    exc = getattr(litellm, kind)(
        message="secret-sentinel", model="test", llm_provider="openai"
    )
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(side_effect=exc))
    result = await health.check_endpoint(TARGET)
    assert result.outcome == "failure"
    assert message in result.error
    assert "secret-sentinel" not in repr(result)


@pytest.mark.asyncio
async def test_missing_configuration_and_programming_error_are_not_outages(monkeypatch):
    import litellm

    call = AsyncMock(side_effect=ValueError("secret-sentinel"))
    monkeypatch.setattr(litellm, "acompletion", call)
    monkeypatch.delenv("TEST_PROVIDER_KEY", raising=False)
    assert (await health.check_endpoint(TARGET)).outcome == "monitor_error"
    call.assert_not_called()
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-sentinel")
    result = await health.check_endpoint(TARGET)
    assert result.outcome == "monitor_error"
    assert "secret-sentinel" not in repr(result)


@pytest.mark.asyncio
async def test_reenable_checks_immediately_and_unknown_config_is_not_claimed(db):
    now = utcnow() + timedelta(seconds=1)
    await save(db, await claim(db, now, [TARGET]), "success", now)
    async with db.session() as session:
        await health.sync_targets(session, [])
        await health.sync_targets(session, [TARGET])
    assert await claim(db, now + timedelta(seconds=1))
    async with db.session() as session:
        assert not await health.claim_checks(session, now + timedelta(hours=1), [])


@pytest.mark.asyncio
async def test_migration_can_downgrade_and_upgrade_and_enables_rls(db):
    def migrate(connection):
        with Operations.context(MigrationContext.configure(connection)):
            db.migration.downgrade()
            db.migration.upgrade()

    async with db.engine.begin() as connection:
        await connection.run_sync(migrate)
        values = (
            (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity FROM pg_class WHERE relname IN ('endpoint_monitors','endpoint_checks')"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert values == [True, True]


def test_preview_monitoring_and_alerts_require_explicit_enablement(monkeypatch):
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-123")
    monkeypatch.delenv("ODDISH_ENABLE_ENDPOINT_MONITORING", raising=False)
    monkeypatch.delenv("ODDISH_ENABLE_SLACK_EXPENSE_NOTIFICATIONS", raising=False)
    monkeypatch.setenv("SLACK_EXPENSE_WEBHOOK_URL", "https://example.invalid")
    assert not health.monitoring_enabled()
    assert not health.alerts_enabled()
    monkeypatch.setenv("ODDISH_ENABLE_ENDPOINT_MONITORING", "true")
    monkeypatch.setenv("ODDISH_ENABLE_SLACK_EXPENSE_NOTIFICATIONS", "true")
    assert health.monitoring_enabled()
    assert health.alerts_enabled()
    monkeypatch.delenv("SLACK_EXPENSE_WEBHOOK_URL")
    assert not health.alerts_enabled()

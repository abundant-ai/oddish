"""Request admission races against disposable PostgreSQL, never a deployed DB."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from oddish.workers.queue import model_capacity as cap
from oddish.workers.queue import model_gateway as gateway


def pool(id="main", **kw):
    return cap.ProviderPool(
        id=id,
        quota_group=id,
        provider="anthropic",
        model="claude-sonnet-5",
        key_env=id.upper() + "_KEY",
        requests_per_minute=100,
        input_tokens_per_minute=1000,
        output_tokens_per_minute=1000,
        **kw,
    )


@pytest_asyncio.fixture
async def db(monkeypatch):
    url = os.environ.get("ODDISH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable ODDISH_TEST_DATABASE_URL")
    schema = "qa_model_" + uuid4().hex
    admin = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url,
        pool_size=12,
        max_overflow=0,
        connect_args={"server_settings": {"search_path": schema}},
    )
    async with engine.begin() as c:
        await c.run_sync(cap.Pool.__table__.create)
        await c.run_sync(cap.Lease.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session():
        async with maker() as s:
            async with s.begin():
                yield s

    monkeypatch.setattr(cap, "get_session", session)
    yield session
    await engine.dispose()
    await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await admin.close()


@pytest.mark.asyncio
async def test_switches_at_65_percent_and_keeps_conversation_affinity(db):
    pools = [pool(), pool("hdo")]
    first = await cap.reserve_request(
        pools, worker_job_id="a", input_tokens=400, output_tokens=1
    )
    boundary = await cap.reserve_request(
        pools, worker_job_id="b", input_tokens=250, output_tokens=1
    )
    overflow = await cap.reserve_request(
        pools, worker_job_id="c", input_tokens=1, output_tokens=1
    )
    assert [r.pool.id for r in (first, boundary, overflow)] == ["main", "main", "hdo"]
    await cap.settle_request(first, usage={"input_tokens": 0, "output_tokens": 0})
    next_call = await cap.reserve_request(
        pools, worker_job_id="c", input_tokens=1, output_tokens=1
    )
    assert next_call.pool.id == "hdo"


@pytest.mark.asyncio
async def test_overlapping_api_replicas_cannot_overspend(db):
    async def launch(i):
        try:
            return await cap.reserve_request(
                [pool(), pool("hdo")],
                worker_job_id=str(i),
                input_tokens=1,
                output_tokens=1,
            )
        except cap.CapacityUnavailable:
            return None

    results = await asyncio.gather(*(launch(i) for i in range(150)))
    admitted = [r for r in results if r]
    assert len(admitted) == 130
    assert sum(r.pool.id == "main" for r in admitted) == 65
    assert sum(r.pool.id == "hdo" for r in admitted) == 65
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(cap.Lease)) == 130


@pytest.mark.asyncio
async def test_pending_stream_and_unknown_failure_keep_reserved_tokens(db):
    reservation = await cap.reserve_request(
        [pool()], worker_job_id="a", input_tokens=650, output_tokens=1
    )
    async with db() as s:
        await s.execute(
            update(cap.Lease).values(created_at=func.now() - timedelta(minutes=3))
        )
        before = (await s.get(cap.Lease, reservation.id)).expires_at
    with pytest.raises(cap.CapacityUnavailable):
        await cap.reserve_request(
            [pool()], worker_job_id="b", input_tokens=1, output_tokens=1
        )
    await cap.settle_request(reservation, usage=None)
    async with db() as s:
        row = await s.get(cap.Lease, reservation.id)
        assert row.expires_at == before and not row.active
    with pytest.raises(cap.CapacityUnavailable):
        await cap.reserve_request(
            [pool()], worker_job_id="b", input_tokens=1, output_tokens=1
        )


@pytest.mark.asyncio
async def test_actual_usage_cache_reads_and_idempotent_settlement(db):
    r = await cap.reserve_request(
        [pool()], worker_job_id="a", input_tokens=650, output_tokens=600
    )
    await cap.settle_request(
        r,
        usage={
            "input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 100000,
            "output_tokens": 40,
        },
    )
    await cap.settle_request(r, usage={"input_tokens": 0, "output_tokens": 0})
    async with db() as s:
        row = await s.get(cap.Lease, r.id)
        assert (row.input_tokens, row.output_tokens, row.active) == (30, 40, False)
    await cap.reserve_request(
        [pool()], worker_job_id="b", input_tokens=620, output_tokens=600
    )


@pytest.mark.asyncio
async def test_abandoned_reservation_and_cooldown_expire(db):
    r = await cap.reserve_request(
        [pool()], worker_job_id="a", input_tokens=650, output_tokens=1
    )
    await cap.observe_provider("main", {}, cooldown_seconds=30)
    async with db() as s:
        await s.execute(
            update(cap.Lease).values(expires_at=func.now() - timedelta(seconds=1))
        )
    with pytest.raises(cap.CapacityUnavailable):
        await cap.reserve_request(
            [pool()], worker_job_id="b", input_tokens=1, output_tokens=1
        )
    async with db() as s:
        await s.execute(
            update(cap.Pool).values(cooldown_until=func.now() - timedelta(seconds=1))
        )
    await cap.reserve_request(
        [pool()], worker_job_id="b", input_tokens=1, output_tokens=1
    )
    await cap.settle_request(r, usage={"input_tokens": 0, "output_tokens": 0})


@pytest.mark.asyncio
async def test_provider_headers_include_other_applications(db):
    r = await cap.reserve_request(
        [pool(), pool("hdo")], worker_job_id="a", input_tokens=1, output_tokens=1
    )
    await cap.observe_provider(
        "main",
        {
            "anthropic-ratelimit-input-tokens-limit": "1000",
            "anthropic-ratelimit-input-tokens-remaining": "350",
        },
    )
    await cap.settle_request(r, usage={"input_tokens": 1, "output_tokens": 1})
    other = await cap.reserve_request(
        [pool(), pool("hdo")], worker_job_id="a", input_tokens=1, output_tokens=1
    )
    assert other.pool.id == "hdo"


@pytest.mark.parametrize("inputs,outputs", [(651, 0), (0, 651)])
@pytest.mark.asyncio
async def test_every_token_dimension_is_enforced(db, inputs, outputs):
    with pytest.raises(cap.CapacityUnavailable):
        await cap.reserve_request(
            [pool()], worker_job_id="a", input_tokens=inputs, output_tokens=outputs
        )


def test_bedrock_uses_weighted_output_accounting():
    p = cap.ProviderPool(
        id="aws",
        quota_group="aws:account:region:sonnet",
        provider="bedrock",
        model="global.anthropic.claude-sonnet-5",
        key_env="AWS_KEY",
        region="us-east-2",
        requests_per_minute=100,
        tokens_per_minute=1000,
        output_multiplier=10,
    )
    assert p.load(1, 100, 55) == 0.65
    assert p.load(1, 100, 56) > 0.65


def test_shared_organization_and_duplicate_keys_are_not_extra_capacity(monkeypatch):
    p = pool()
    h = pool("hdo").model_copy(update={"quota_group": "main"})
    monkeypatch.setenv(
        "ODDISH_QA_MODEL_POOLS", json.dumps([p.model_dump(), h.model_dump()])
    )
    with pytest.raises(ValueError, match="distinct quota_group"):
        cap.configured_pools()
    monkeypatch.setenv(
        "ODDISH_QA_MODEL_POOLS", json.dumps([p.model_dump(), pool("hdo").model_dump()])
    )
    monkeypatch.setenv("MAIN_KEY", "same")
    monkeypatch.setenv("HDO_KEY", "same")
    with pytest.raises(ValueError, match="duplicate credentials"):
        cap.configured_pools()
    monkeypatch.delenv("HDO_KEY")
    assert cap.configured_pools() == [p]


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "agent"},
        {"byok_env": {"ANTHROPIC_API_KEY": "user"}},
        {"agent": "codex"},
        {"model": "claude-opus-4-6"},
        {"harbor_config": {"variant_id": "ephemeral"}},
    ],
)
def test_unrelated_trials_are_unchanged(monkeypatch, change):
    args = dict(
        kind="qa",
        agent="claude-code",
        model="global.anthropic.claude-sonnet-5",
        byok_env=None,
        harbor_config={},
    )
    assert gateway.is_gateway_trial(**args)
    assert not gateway.is_gateway_trial(**{**args, **change})


@pytest.mark.parametrize(
    "model", ["global.anthropic.claude-sonnet-5", "anthropic-hdo/claude-sonnet-5"]
)
def test_gateway_env_wins_over_ambient_provider_credentials(monkeypatch, model):
    from oddish.workers.harbor.agent_config import _build_agent_config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "platform-secret")
    monkeypatch.setenv("ANTHROPIC_HDO_API_KEY", "hdo-secret")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    env = {
        "ODDISH_QA_MODEL_ROUTED": "1",
        "ANTHROPIC_BASE_URL": "https://gateway.example/qa-model",
        "ANTHROPIC_API_KEY": "worker.token",
        "ANTHROPIC_AUTH_TOKEN": "",
        "ANTHROPIC_MODEL": "claude-sonnet-5",
        "CLAUDE_CODE_USE_BEDROCK": "",
        "AWS_BEARER_TOKEN_BEDROCK": "",
        "CLAUDE_CODE_SUBAGENT_MODEL": "claude-sonnet-5",
    }
    config = _build_agent_config(
        agent="claude-code",
        model=model,
        raw_harbor_config={},
        is_probe=True,
        probe_oddish_env=env,
    )
    assert config.model_name == "claude-sonnet-5"
    for key, value in env.items():
        assert config.env[key] == value


@pytest.mark.asyncio
async def test_capacity_snapshot_distinguishes_workers_from_requests(db):
    r = await cap.reserve_request(
        [pool()], worker_job_id="one-worker", input_tokens=1, output_tokens=1
    )
    await cap.reserve_request(
        [pool()], worker_job_id="one-worker", input_tokens=1, output_tokens=1
    )
    snapshot = (await cap.capacity_snapshot([pool()]))[0]
    assert snapshot["active_requests"] == 2 and snapshot["accepting_requests"]
    await cap.settle_request(r, usage={"input_tokens": 1, "output_tokens": 1})
    snapshot = (await cap.capacity_snapshot([pool()]))[0]
    assert (
        snapshot["active_requests"] == 1
        and snapshot["reserved_or_recent_requests"] == 2
    )


@pytest.mark.asyncio
async def test_gateway_token_is_minted_only_for_owned_attempt(monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy.dialects import postgresql

    monkeypatch.setenv("ODDISH_QA_MODEL_GATEWAY_URL", "https://dedicated.example")
    monkeypatch.setattr(gateway, "configured_pools", lambda: [pool()])
    statements = []

    class Session:
        matched = 1

        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(rowcount=self.matched)

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(gateway, "get_session", session)
    env = await gateway.mint_gateway_env("worker", 3)
    assert env["ANTHROPIC_BASE_URL"] == "https://dedicated.example/qa-model"
    assert env["ANTHROPIC_API_KEY"].startswith("worker.")
    query = statements[0].compile(dialect=postgresql.dialect())
    assert "worker_jobs.attempts =" in str(query) and "worker_jobs.status =" in str(
        query
    )
    assert "job_token_hash" in query.params
    assert env["ANTHROPIC_API_KEY"] not in str(query.params)
    Session.matched = 0
    with pytest.raises(RuntimeError, match="inactive worker attempt"):
        await gateway.mint_gateway_env("worker", 3)


@pytest.mark.asyncio
async def test_renamed_route_in_another_replica_shares_account_budget(db):
    original = pool().model_copy(update={"quota_group": "anthropic:org-1:sonnet-5"})
    renamed = pool("renamed_main").model_copy(
        update={"quota_group": original.quota_group}
    )
    await cap.reserve_request(
        [original], worker_job_id="a", input_tokens=650, output_tokens=1
    )
    with pytest.raises(cap.CapacityUnavailable):
        await cap.reserve_request(
            [renamed], worker_job_id="b", input_tokens=1, output_tokens=1
        )
    snapshot = (await cap.capacity_snapshot([renamed]))[0]
    assert snapshot["active_requests"] == 1
    assert snapshot["routing_load"] == 0.65


@pytest.mark.asyncio
async def test_fractional_external_reserve_combines_with_database_token_totals(db):
    p = pool(external_load_fraction=0.1)
    await cap.reserve_request([p], worker_job_id="a", input_tokens=500, output_tokens=1)
    snapshot = (await cap.capacity_snapshot([p]))[0]
    assert snapshot["routing_load"] == pytest.approx(0.6)
    with pytest.raises(cap.CapacityUnavailable):
        await cap.reserve_request(
            [p], worker_job_id="b", input_tokens=60, output_tokens=1
        )

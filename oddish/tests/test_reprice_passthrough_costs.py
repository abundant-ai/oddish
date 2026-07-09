from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import oddish.reprice_passthrough_costs as reprice_module
from oddish.model_pricing import untrusted_native_cost_providers
from oddish.reprice_passthrough_costs import (
    _SELECT_CANDIDATES,
    _UPDATE_COST,
    _plan_reprice,
    parse_since,
)


def test_positive_untrusted_native_cost_is_replaced_by_token_estimate() -> None:
    plan = _plan_reprice(
        agent="claude-code",
        provider="fireworks",
        model="fireworks/minimax-m3",
        existing_cost=99.0,
        input_tokens=2_000_000,
        cache_tokens=1_000_000,
        cache_write_tokens=0,
        output_tokens=100_000,
    )

    assert plan.skip_reason is None
    assert plan.estimated_cost == pytest.approx(
        1_000_000 * 3e-7 + 1_000_000 * 6e-8 + 100_000 * 1.2e-6
    )
    assert plan.should_update


@pytest.mark.parametrize("existing_cost", [None, 0.0])
def test_null_and_zero_untrusted_costs_are_repriced(
    existing_cost: float | None,
) -> None:
    plan = _plan_reprice(
        agent="claude-code",
        provider="minimax",
        model="minimax/minimax-m3",
        existing_cost=existing_cost,
        input_tokens=1_000,
        cache_tokens=0,
        cache_write_tokens=0,
        output_tokens=100,
    )

    assert plan.skip_reason is None
    assert plan.estimated_cost == pytest.approx(1_000 * 3e-7 + 100 * 1.2e-6)
    assert plan.should_update


def test_unpriceable_passthrough_cost_is_reported_without_a_write() -> None:
    plan = _plan_reprice(
        agent="claude-code",
        provider="moonshot",
        model="moonshot/unknown-model",
        existing_cost=42.0,
        input_tokens=1_000,
        cache_tokens=0,
        cache_write_tokens=0,
        output_tokens=100,
    )

    assert plan.estimated_cost is None
    assert plan.skip_reason == "unpriceable"
    assert not plan.should_update


@pytest.mark.parametrize(
    ("agent", "provider"),
    [
        ("claude-code", "anthropic"),
        ("claude-code", "bedrock"),
        ("mini-swe-agent", "fireworks"),
    ],
)
def test_trusted_native_cost_routes_are_excluded(agent: str, provider: str) -> None:
    plan = _plan_reprice(
        agent=agent,
        provider=provider,
        model="fireworks/minimax-m3",
        existing_cost=99.0,
        input_tokens=1_000,
        cache_tokens=0,
        cache_write_tokens=0,
        output_tokens=100,
    )

    assert plan.estimated_cost is None
    assert plan.skip_reason == "trusted-native-cost"
    assert not plan.should_update


def test_candidate_sql_includes_historical_first_party_and_token_guards() -> None:
    sql = " ".join(_SELECT_CANDIDATES.text.lower().split())

    assert "t.finished_at >= :since" in sql
    assert "lower(t.agent) like '%claude-code%'" in sql
    assert "lower(t.provider) in :passthrough_providers" in sql
    assert "t.origin::text = 'oddish'" in sql
    assert "t.idempotency_key not like 'combine:%'" in sql
    assert "t.billed_user_id is not null or t.is_probe is false" in sql
    assert "coalesce(t.input_tokens, 0) > 0" in sql
    assert "cost_usd = 0" not in sql
    assert "cost_usd is null" not in sql


def test_update_sql_uses_null_safe_old_cost_concurrency_guard() -> None:
    sql = " ".join(_UPDATE_COST.text.lower().split())

    assert "cost_usd is not distinct from :old_cost" in sql
    assert "set cost_usd = :new_cost" in sql


def test_parse_since_accepts_iso_dates_and_normalizes_naive_values_to_utc() -> None:
    assert parse_since("2026-07-01").isoformat() == "2026-07-01T00:00:00+00:00"
    assert parse_since("2026-07-01T03:04:05Z").isoformat() == (
        "2026-07-01T03:04:05+00:00"
    )


@pytest.mark.asyncio
async def test_dry_run_is_default_and_never_executes_an_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[object] = []
    select_count = 0
    selected_row = SimpleNamespace(
        id="task-1-0",
        agent="claude-code",
        provider="fireworks",
        model="fireworks/minimax-m3",
        cost_usd=99.0,
        input_tokens=1_000,
        output_tokens=100,
        cache_tokens=0,
        cache_write_tokens=0,
    )

    class FakeResult:
        def __init__(self, rows: list[object]) -> None:
            self.rows = rows

        def fetchall(self) -> list[object]:
            return self.rows

    class FakeSession:
        async def execute(self, statement, parameters):
            nonlocal select_count
            statements.append(statement)
            assert parameters["passthrough_providers"] == tuple(
                sorted(untrusted_native_cost_providers(agent="claude-code"))
            )
            select_count += 1
            return FakeResult([selected_row] if select_count == 1 else [])

    @asynccontextmanager
    async def fake_get_session():
        yield FakeSession()

    monkeypatch.setattr(reprice_module, "get_session", fake_get_session)

    await reprice_module.run_reprice(since="2026-01-01")

    assert statements == [_SELECT_CANDIDATES, _SELECT_CANDIDATES]

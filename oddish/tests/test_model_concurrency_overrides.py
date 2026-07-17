from types import SimpleNamespace

import pytest

from oddish.config import settings
from oddish.core.model_concurrency import (
    get_effective_model_concurrency_limits,
    set_model_concurrency_override,
)
from oddish.workers.queue.worker_job_dispatcher import build_spawn_plan


class _Session:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return SimpleNamespace(all=lambda: self.rows)


@pytest.mark.parametrize("limit", [-1, 10_001])
@pytest.mark.asyncio
async def test_set_model_concurrency_rejects_out_of_range(limit):
    with pytest.raises(ValueError, match="between 0 and 10000"):
        await set_model_concurrency_override(_Session(), "openai/gpt-5.4-mini", limit)


@pytest.mark.asyncio
async def test_effective_limits_prefer_database_override():
    session = _Session(
        [SimpleNamespace(queue_key="minimax/minimax-m3", concurrency_limit=72)]
    )

    limits = await get_effective_model_concurrency_limits(
        session, ["MiniMax/MiniMax-M3", "unconfigured/model"]
    )

    # Keyed by what the caller passed; the override still matches through
    # normalization rather than by raw string equality.
    assert limits["MiniMax/MiniMax-M3"] == 72
    assert limits["unconfigured/model"] > 0
    assert session.calls[0][1] == {
        "queue_keys": ["minimax/minimax-m3", "unconfigured/model"]
    }


@pytest.mark.asyncio
async def test_effective_limits_are_keyed_by_the_callers_queue_key():
    """``build_spawn_plan`` reads a missing key as limit 0 and silently stalls
    the queue, so the dict must key off the caller's spelling even where
    ``normalize_queue_key`` rewrites it (a Claude alias -> a Bedrock id).
    """
    raw = "anthropic/claude-haiku-4-5-20251001"
    assert settings.normalize_queue_key(raw) != raw, "picked a key that isn't rewritten"

    limits = await get_effective_model_concurrency_limits(_Session(), (raw,))

    assert raw in limits and limits[raw] > 0
    plan = build_spawn_plan(
        queued_by_org_queue={("org1", raw, "default"): 5},
        running_by_queue={},
        concurrency_limits=limits,
        max_workers=10,
    )
    assert plan, "queued work under a positive limit must still spawn workers"


@pytest.mark.asyncio
async def test_zero_override_is_an_off_switch():
    session = _Session(
        [SimpleNamespace(queue_key="minimax/minimax-m3", concurrency_limit=0)]
    )

    limits = await get_effective_model_concurrency_limits(
        session, ("minimax/minimax-m3",)
    )

    assert limits["minimax/minimax-m3"] == 0
    plan = build_spawn_plan(
        queued_by_org_queue={("org1", "minimax/minimax-m3", "default"): 5},
        running_by_queue={},
        concurrency_limits=limits,
        max_workers=10,
    )
    assert plan == [], "a 0 override must stop the queue dead"


@pytest.mark.asyncio
async def test_set_and_clear_override():
    session = _Session()

    assert await set_model_concurrency_override(session, "MiniMax/MiniMax-M3", 96) == (
        "minimax/minimax-m3",
        96,
    )
    assert "INSERT INTO model_concurrency_overrides" in session.calls[0][0]

    assert await set_model_concurrency_override(
        session, "MiniMax/MiniMax-M3", None
    ) == ("minimax/minimax-m3", None)
    assert "DELETE FROM model_concurrency_overrides" in session.calls[1][0]

from types import SimpleNamespace

import pytest

from oddish.core.model_concurrency import (
    get_effective_model_concurrency_limits,
    set_model_concurrency_override,
)


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

    assert limits["minimax/minimax-m3"] == 72
    assert limits["unconfigured/model"] > 0
    assert session.calls[0][1] == {
        "queue_keys": ["minimax/minimax-m3", "unconfigured/model"]
    }


@pytest.mark.asyncio
async def test_set_and_clear_override():
    session = _Session()

    assert await set_model_concurrency_override(session, "MiniMax/MiniMax-M3", 96) == (
        "minimax/minimax-m3",
        96,
        96,
    )
    assert "INSERT INTO model_concurrency_overrides" in session.calls[0][0]

    queue_key, effective, override = await set_model_concurrency_override(
        session, "MiniMax/MiniMax-M3", None
    )
    assert queue_key == "minimax/minimax-m3"
    assert effective > 0
    assert override is None
    assert "DELETE FROM model_concurrency_overrides" in session.calls[1][0]

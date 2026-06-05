from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
asyncpg_module = ModuleType("asyncpg")
asyncpg_module.Connection = object
sys.modules.setdefault("asyncpg", asyncpg_module)
config_module = ModuleType("oddish.config")
config_module.settings = SimpleNamespace(
    asyncpg_url="postgresql://example",
    asyncpg_server_settings=lambda: {},
)
sys.modules.setdefault("oddish.config", config_module)

_SLOTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "oddish"
    / "workers"
    / "queue"
    / "slots.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "oddish_queue_slots_test_module", _SLOTS_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
slots = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(slots)


def _normalized_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


@pytest.mark.asyncio
async def test_orphaned_slot_cleanup_matches_exact_running_owner(monkeypatch):
    calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeConnection:
        async def execute(self, sql: str, *args: object) -> str:
            calls.append((sql, args))
            return "UPDATE 3"

    @asynccontextmanager
    async def fake_slot_connection():
        yield FakeConnection()

    monkeypatch.setattr(slots, "_slot_connection", fake_slot_connection)

    count = await slots.cleanup_orphaned_queue_slots("model-key")

    assert count == 3
    assert calls
    sql, args = calls[0]
    normalized = _normalized_sql(sql)
    assert args == ("model-key",)
    assert "AND wj.queue_key = qs.queue_key" in normalized
    assert "AND wj.current_worker_id = qs.locked_by" in normalized
    assert "AND wj.current_queue_slot = qs.slot" in normalized

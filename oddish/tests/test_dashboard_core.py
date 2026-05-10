from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import dashboard  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_dashboard_cache():
    dashboard._dashboard_cache.clear()
    yield
    dashboard._dashboard_cache.clear()


@pytest.mark.asyncio
async def test_experiments_only_dashboard_skips_queue_stats(monkeypatch):
    async def fail_queue_stats(*args, **kwargs):
        raise AssertionError("experiments-only requests should not load queue stats")

    async def load_experiments(session, **kwargs):
        return ([{"id": "exp-1", "name": "Experiment 1"}], False)

    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr(
        dashboard,
        "get_queue_and_pipeline_stats_with_concurrency",
        fail_queue_stats,
    )
    monkeypatch.setattr(dashboard, "load_dashboard_experiments", load_experiments)
    monkeypatch.setattr(dashboard, "get_session", fake_session)

    response = await dashboard.get_dashboard_core(
        object(),  # type: ignore[arg-type]
        include_tasks=False,
        include_usage=False,
        include_experiments=True,
    )

    assert response["queues"] == {}
    assert response["pipeline"] == {"trials": {}, "analyses": {}, "verdicts": {}}
    assert response["experiments"] == [{"id": "exp-1", "name": "Experiment 1"}]


@pytest.mark.asyncio
async def test_queue_only_dashboard_still_loads_queue_stats(monkeypatch):
    async def load_queue_stats(session, org_id=None):
        return (
            {"openai/gpt-5": {"queued": 1, "running": 0}},
            {"trials": {"queued": 1}, "analyses": {}, "verdicts": {}},
        )

    monkeypatch.setattr(
        dashboard,
        "get_queue_and_pipeline_stats_with_concurrency",
        load_queue_stats,
    )

    response = await dashboard.get_dashboard_core(
        object(),  # type: ignore[arg-type]
        include_tasks=False,
        include_usage=False,
        include_experiments=False,
    )

    assert response["queues"] == {"openai/gpt-5": {"queued": 1, "running": 0}}
    assert response["pipeline"] == {
        "trials": {"queued": 1},
        "analyses": {},
        "verdicts": {},
    }
    assert response["experiments"] == []

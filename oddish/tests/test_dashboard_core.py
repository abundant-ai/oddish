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

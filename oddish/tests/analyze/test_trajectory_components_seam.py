import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.workers.queue import analysis_handler as analysis
from oddish.workers.queue import trajectory_summary_handler as summary_worker


@pytest.fixture(autouse=True)
def _summary_seam(monkeypatch):
    saved = summary_worker._trajectory_summary_provider
    trial = SimpleNamespace(id="t1")
    session = SimpleNamespace()

    async def get(*_args, **_kwargs):
        return trial

    session.get = get

    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(summary_worker, "get_session", get_session)
    monkeypatch.setattr(summary_worker, "_has_fetchable_trajectory", lambda _: True)
    yield
    summary_worker._trajectory_summary_provider = saved


@pytest.mark.asyncio
async def test_no_provider_returns_none():
    summary_worker._trajectory_summary_provider = None
    assert await summary_worker.resolve_trajectory_components("t1") is None


@pytest.mark.asyncio
async def test_qa_can_generate_and_extract_components_best_effort():
    async def provider(trial_id, triggered_by_user_id):
        assert trial_id == "t1"
        assert triggered_by_user_id is None
        return {
            "schema_version": "5",
            "components": [{"trajectory_component": "debugging"}],
        }

    summary_worker.register_trajectory_summary_provider(provider)
    assert await summary_worker.resolve_trajectory_components("t1") == [
        {"trajectory_component": "debugging"}
    ]


@pytest.mark.asyncio
async def test_provider_failure_is_best_effort():
    async def provider(_trial_id, _triggered_by_user_id):
        raise RuntimeError("gen failed")

    summary_worker.register_trajectory_summary_provider(provider)
    assert await summary_worker.resolve_trajectory_components("t1") is None


@pytest.mark.asyncio
async def test_summary_without_components_returns_none():
    async def provider(_trial_id, _triggered_by_user_id):
        return {"schema_version": "5", "summary": "s"}

    summary_worker.register_trajectory_summary_provider(provider)
    assert await summary_worker.resolve_trajectory_components("t1") is None


@pytest.mark.asyncio
async def test_hung_provider_is_bounded(monkeypatch):
    monkeypatch.setattr(summary_worker, "TRAJECTORY_SUMMARY_TIMEOUT_SECONDS", 0.05)

    async def provider(_trial_id, _triggered_by_user_id):
        await asyncio.sleep(3600)

    summary_worker.register_trajectory_summary_provider(provider)
    resolved = await asyncio.wait_for(
        summary_worker.resolve_trajectory_components("t1"), timeout=10
    )
    assert resolved is None


def test_bounded_phases_fit_inside_analysis_claim_ttl():
    bounded = (
        analysis.ANALYSIS_TIMEOUT + summary_worker.TRAJECTORY_SUMMARY_TIMEOUT_SECONDS
    )
    assert bounded < analysis.ANALYSIS_CLAIM_TTL_MINUTES * 60

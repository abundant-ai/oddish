import pytest

from oddish.workers.queue import analysis_handler as ah


@pytest.fixture(autouse=True)
def _restore_provider():
    saved = ah._trajectory_summary_provider
    yield
    ah._trajectory_summary_provider = saved


@pytest.mark.asyncio
async def test_no_provider_returns_none():
    ah._trajectory_summary_provider = None
    assert await ah._resolve_trajectory_components("t1") is None


@pytest.mark.asyncio
async def test_provider_components_extracted():
    async def fake(trial_id):
        assert trial_id == "t1"
        return {"components": [{"trajectory_component": "debugging"}]}

    ah.register_trajectory_summary_provider(fake)
    assert await ah._resolve_trajectory_components("t1") == [
        {"trajectory_component": "debugging"}
    ]


@pytest.mark.asyncio
async def test_provider_failure_is_best_effort():
    async def boom(trial_id):
        raise RuntimeError("gen failed")

    ah.register_trajectory_summary_provider(boom)
    assert await ah._resolve_trajectory_components("t1") is None


@pytest.mark.asyncio
async def test_summary_without_components_returns_none():
    async def fake(trial_id):
        return {"summary": "s"}

    ah.register_trajectory_summary_provider(fake)
    assert await ah._resolve_trajectory_components("t1") is None

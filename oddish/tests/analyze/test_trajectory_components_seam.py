import asyncio

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


@pytest.mark.asyncio
async def test_hung_provider_is_bounded(monkeypatch):
    """A hung summary generation must not run out the analysis claim TTL."""
    monkeypatch.setattr(ah, "TRAJECTORY_SUMMARY_TIMEOUT_SECONDS", 0.05, raising=False)

    async def hang(trial_id):
        await asyncio.sleep(3600)

    ah.register_trajectory_summary_provider(hang)
    resolved = await asyncio.wait_for(
        ah._resolve_trajectory_components("t1"), timeout=10
    )
    assert resolved is None


def test_bounded_phases_fit_inside_the_claim_ttl():
    """The claim TTL must budget for every phase it now covers.

    Classification and summary generation are the two bounded phases inside a
    claim; S3 download and sandbox provisioning share whatever is left. If a
    future edit grows either timeout past the TTL, a live worker's claim
    expires mid-run and a peer retakes the trial.
    """
    bounded = ah.ANALYSIS_TIMEOUT + ah.TRAJECTORY_SUMMARY_TIMEOUT_SECONDS
    assert bounded < ah.ANALYSIS_CLAIM_TTL_MINUTES * 60

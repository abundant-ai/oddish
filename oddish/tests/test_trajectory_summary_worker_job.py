from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish import queue
from oddish.workers.queue import trajectory_summary_handler as summary_worker


@pytest.fixture(autouse=True)
def _restore_provider():
    saved = summary_worker._trajectory_summary_provider
    yield
    summary_worker._trajectory_summary_provider = saved


def _session_factory(trial):
    session = SimpleNamespace(get=lambda *_args, **_kwargs: None)

    async def _get(*_args, **_kwargs):
        return trial

    session.get = _get

    @asynccontextmanager
    async def _get_session():
        yield session

    return _get_session


@pytest.mark.asyncio
async def test_success_returns_provider_stored_summary_and_viewer(monkeypatch):
    trial = SimpleNamespace(id="trial-1")
    monkeypatch.setattr(summary_worker, "get_session", _session_factory(trial))
    monkeypatch.setattr(summary_worker, "_has_fetchable_trajectory", lambda _: True)
    seen = {}

    async def provider(trial_id, triggered_by_user_id):
        seen.update(trial_id=trial_id, viewer=triggered_by_user_id)
        return {"schema_version": "5", "summary": "stored"}

    summary_worker.register_trajectory_summary_provider(provider)
    result = await summary_worker.run_trajectory_summary_job(
        "trial-1",
        schema_version="5",
        triggered_by_user_id="viewer-7",
    )
    assert result["summary"] == "stored"
    assert seen == {"trial_id": "trial-1", "viewer": "viewer-7"}


@pytest.mark.asyncio
async def test_generation_failure_propagates_for_worker_retry(monkeypatch):
    trial = SimpleNamespace(id="trial-1")
    monkeypatch.setattr(summary_worker, "get_session", _session_factory(trial))
    monkeypatch.setattr(summary_worker, "_has_fetchable_trajectory", lambda _: True)

    async def provider(_trial_id, _triggered_by_user_id):
        raise RuntimeError("provider unavailable")

    summary_worker.register_trajectory_summary_provider(provider)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await summary_worker.run_trajectory_summary_job(
            "trial-1", schema_version="5", triggered_by_user_id=None
        )


@pytest.mark.asyncio
async def test_generation_timeout_propagates_for_worker_retry(monkeypatch):
    trial = SimpleNamespace(id="trial-1")
    monkeypatch.setattr(summary_worker, "get_session", _session_factory(trial))
    monkeypatch.setattr(summary_worker, "_has_fetchable_trajectory", lambda _: True)
    monkeypatch.setattr(summary_worker, "TRAJECTORY_SUMMARY_TIMEOUT_SECONDS", 0.01)

    async def provider(_trial_id, _triggered_by_user_id):
        await asyncio.sleep(3600)

    summary_worker.register_trajectory_summary_provider(provider)
    with pytest.raises(TimeoutError):
        await summary_worker.run_trajectory_summary_job(
            "trial-1", schema_version="5", triggered_by_user_id=None
        )


@pytest.mark.asyncio
async def test_missing_trial_is_permanent(monkeypatch):
    monkeypatch.setattr(summary_worker, "get_session", _session_factory(None))
    with pytest.raises(
        summary_worker.TrajectorySummaryUnavailableError, match="not found"
    ):
        await summary_worker.run_trajectory_summary_job(
            "missing", schema_version="5", triggered_by_user_id=None
        )


@pytest.mark.asyncio
async def test_trial_without_trajectory_is_permanent(monkeypatch):
    trial = SimpleNamespace(id="trial-1")
    monkeypatch.setattr(summary_worker, "get_session", _session_factory(trial))
    monkeypatch.setattr(summary_worker, "_has_fetchable_trajectory", lambda _: False)
    with pytest.raises(
        summary_worker.TrajectorySummaryUnavailableError, match="no trajectory"
    ):
        await summary_worker.run_trajectory_summary_job(
            "trial-1", schema_version="5", triggered_by_user_id=None
        )


@pytest.mark.asyncio
async def test_enqueue_upsert_returns_the_constraint_winner(monkeypatch):
    winner = SimpleNamespace(id="job-winner")
    session = SimpleNamespace()

    async def scalar(statement):
        sql = str(statement)
        assert "ON CONFLICT" in sql
        return winner.id

    async def get(_model, job_id):
        assert job_id == winner.id
        return winner

    session.scalar = scalar
    session.get = get
    monkeypatch.setattr(queue, "_wake_dispatcher", lambda: None)
    result = await queue.enqueue_trajectory_summary_worker_job(
        session,
        trial_id="trial-1",
        org_id="org-1",
        schema_version="5",
        triggered_by_user_id="viewer-7",
    )
    assert result is winner


@pytest.mark.asyncio
async def test_repeated_enqueue_reuses_active_database_job(session):
    first = await queue.enqueue_trajectory_summary_worker_job(
        session,
        trial_id="summary-dedupe-trial",
        org_id="summary-dedupe-org",
        schema_version="5",
        triggered_by_user_id="viewer-1",
    )
    second = await queue.enqueue_trajectory_summary_worker_job(
        session,
        trial_id="summary-dedupe-trial",
        org_id="summary-dedupe-org",
        schema_version="5",
        triggered_by_user_id="viewer-2",
    )
    assert second.id == first.id
    assert second.payload["triggered_by_user_id"] == "viewer-1"

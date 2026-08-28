from __future__ import annotations

import pytest

import oddish.core.dashboard as dashboard


@pytest.mark.asyncio
async def test_dashboard_experiments_reuse_caller_session(monkeypatch) -> None:
    caller_session = object()
    seen_sessions: list[object] = []

    async def _load_experiments(session, **_kwargs):
        seen_sessions.append(session)
        return [], False

    monkeypatch.setattr(dashboard, "load_dashboard_experiments", _load_experiments)
    dashboard.invalidate_dashboard_cache()

    response = await dashboard.get_dashboard_core(
        caller_session,  # type: ignore[arg-type]
        org_id="test-dashboard-session-ownership",
        include_queues=False,
        include_tasks=False,
        include_usage=False,
        include_experiments=True,
    )

    assert seen_sessions == [caller_session]
    assert response["experiments"] == []
    assert response["cached"] is False

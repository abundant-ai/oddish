"""Tests for the probe-submission cutover in POST /api/tasks/sweep.

When ``extra_instructions`` is set, ``create_task_sweep`` must:
  1. Forward the submission to ``AgentSandboxClient.submit_probe``.
  2. NOT call ``create_task_sweep_core`` (no local TrialModel rows).
  3. Return a ``TaskResponse`` with ``new_trial_ids=[probe_run_id]``.

Also tests ``list_task_probe_runs``:
  - Unions legacy probe TrialModel rows with ExternalProbeRun service rows.
  - Sorts by created_at descending.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_auth(org_id: str = "org_a", user_id: str = "user_b") -> MagicMock:
    auth = MagicMock()
    auth.org_id = org_id
    auth.user_id = user_id
    auth.require_scope = MagicMock()
    return auth


def _make_request(client=None) -> MagicMock:
    request = MagicMock()
    request.app.state.agent_sandbox_client = client
    return request


def _make_submission(
    *,
    task_id: str = "task_1",
    extra_instructions: str | None = None,
    agent: str = "claude-code",
    model: str = "claude-sonnet-4-6",
) -> MagicMock:
    config = MagicMock()
    config.agent = agent
    config.model = model

    sub = MagicMock()
    sub.task_id = task_id
    sub.extra_instructions = extra_instructions
    sub.configs = [config]
    sub.priority = MagicMock()
    sub.name = None
    return sub


# ---------------------------------------------------------------------------
# create_task_sweep: probe path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_submission_calls_submit_probe():
    """When extra_instructions is set, submit_probe is called with the right args."""
    from api.routers.tasks import _create_probe_sweep
    from oddish.db.models import Priority, TaskStatus

    fake_client = MagicMock()
    fake_client.submit_probe = AsyncMock(return_value="pr_abc")

    auth = _make_auth()
    request = _make_request(client=fake_client)
    submission = _make_submission(
        task_id="task_1",
        extra_instructions="Try to cheat",
        agent="claude-code",
        model="claude-sonnet-4-6",
    )
    submission.priority = Priority.LOW

    # Patch get_session to avoid real DB
    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.add = MagicMock()
    fake_session.commit = AsyncMock()
    fake_session.get = AsyncMock(return_value=None)  # no TaskModel row

    with patch("api.routers.tasks.get_session", return_value=fake_session):
        response = await _create_probe_sweep(submission, request, auth)

    fake_client.submit_probe.assert_called_once_with(
        org_id="org_a",
        user_id="user_b",
        task_id="task_1",
        agent="claude-code",
        model="claude-sonnet-4-6",
        extra_instructions="Try to cheat",
        timeout_minutes=30,
    )

    assert response.new_trial_ids == ["pr_abc"]
    assert response.trials_count == 1


@pytest.mark.asyncio
async def test_probe_submission_returns_503_when_client_not_configured():
    """When no agent_sandbox_client is configured, return 503."""
    from fastapi import HTTPException

    from api.routers.tasks import _create_probe_sweep

    request = _make_request(client=None)
    auth = _make_auth()
    submission = _make_submission(extra_instructions="Try to cheat")

    with pytest.raises(HTTPException) as exc_info:
        await _create_probe_sweep(submission, request, auth)

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_non_probe_submission_does_not_call_submit_probe():
    """When extra_instructions is None, the normal sweep path is taken."""
    from api.routers.tasks import create_task_sweep
    from oddish.db.models import Priority, TaskStatus

    fake_client = MagicMock()
    fake_client.submit_probe = AsyncMock(return_value="pr_xyz")

    auth = _make_auth()
    request = _make_request(client=fake_client)
    submission = _make_submission(extra_instructions=None)
    submission.priority = Priority.LOW
    submission.github_username = None
    submission.publish_experiment = None

    # Patch the non-probe code path to short-circuit it.
    with patch("oddish.core.sweeps.validate_sweep_submission"), patch(
        "api.routers.tasks._apply_github_attribution"
    ), patch("api.routers.tasks.create_task_sweep_core") as mock_core, patch(
        "api.routers.tasks.get_session"
    ) as mock_session:
        fake_s = AsyncMock()
        fake_s.__aenter__ = AsyncMock(return_value=fake_s)
        fake_s.__aexit__ = AsyncMock(return_value=False)
        fake_s.commit = AsyncMock()
        mock_session.return_value = fake_s

        fake_task = MagicMock()
        fake_task.id = "task_1"
        fake_task.name = "task_1"
        fake_task.status = TaskStatus.PENDING
        fake_task.priority = Priority.LOW
        fake_task.trials = []
        fake_task.experiments = []
        fake_task.created_at = datetime.now(timezone.utc)
        mock_core.return_value = (fake_task, [], False, None)

        await create_task_sweep(submission, request, auth)

    fake_client.submit_probe.assert_not_called()


# ---------------------------------------------------------------------------
# list_task_probe_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_task_probe_runs_unions_legacy_and_service():
    """list_task_probe_runs returns both legacy probe trials and service rows."""
    from api.routers.tasks import list_task_probe_runs

    now = datetime.now(timezone.utc)
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # Legacy trial with mode=probe
    legacy_trial = MagicMock()
    legacy_trial.id = "t-legacy-1"
    legacy_trial.task_id = "task_1"
    legacy_trial.harbor_config = {"mode": "probe"}
    legacy_trial.analysis_status = None
    legacy_trial.analysis = None
    legacy_trial.created_at = earlier

    # External probe run row
    ext_row = MagicMock()
    ext_row.id = "pr_service_1"

    # Service response for get_probe
    service_envelope = {
        "probe_run_id": "pr_service_1",
        "status": "success",
        "task_id": "task_1",
        "created_at": now.isoformat(),
        "analyzer_result": {"headline": "ok"},
    }

    fake_client = MagicMock()
    fake_client.get_probe = AsyncMock(return_value=service_envelope)

    auth = _make_auth()
    request = _make_request(client=fake_client)

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    execute_results = [
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[legacy_trial])))),
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[ext_row])))),
    ]
    fake_session.execute = AsyncMock(side_effect=execute_results)

    with patch("api.routers.tasks.get_session", return_value=fake_session):
        result = await list_task_probe_runs("task_1", auth, request)

    assert len(result) == 2
    kinds = {r["kind"] for r in result}
    assert kinds == {"legacy", "service"}


@pytest.mark.asyncio
async def test_list_task_probe_runs_skips_non_probe_trials():
    """TrialModel rows without mode=probe are excluded."""
    from api.routers.tasks import list_task_probe_runs

    non_probe_trial = MagicMock()
    non_probe_trial.id = "t-normal-1"
    non_probe_trial.task_id = "task_1"
    non_probe_trial.harbor_config = {}  # no mode=probe
    non_probe_trial.analysis_status = None
    non_probe_trial.analysis = None
    non_probe_trial.created_at = datetime.now(timezone.utc)

    auth = _make_auth()
    request = _make_request(client=None)

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    execute_results = [
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[non_probe_trial])))),
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
    ]
    fake_session.execute = AsyncMock(side_effect=execute_results)

    with patch("api.routers.tasks.get_session", return_value=fake_session):
        result = await list_task_probe_runs("task_1", auth, request)

    assert result == []

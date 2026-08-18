from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services.summarize_trajectory import (
    SCHEMA_VERSION,
    get_or_enqueue_summary_job,
)
from oddish.db.models import WorkerJobKind, WorkerJobStatus


def _result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


@pytest.mark.asyncio
async def test_summary_enqueue_reuses_terminal_job_for_same_trial_and_schema():
    failed = SimpleNamespace(id="job-old", status=WorkerJobStatus.FAILED)
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(), _result(failed)])
    trial = SimpleNamespace(id="trial-1", org_id="org-1")

    job = await get_or_enqueue_summary_job(session, trial)

    assert job is failed
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_summary_enqueue_creates_trial_scoped_schema_keyed_job():
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[MagicMock(), _result(None)])
    session.flush = AsyncMock()
    trial = SimpleNamespace(id="trial-1", org_id="org-1")

    job = await get_or_enqueue_summary_job(session, trial)

    assert job.kind == WorkerJobKind.ANALYZER
    assert job.subject_table == "trials"
    assert job.subject_id == "trial-1"
    assert job.org_id == "org-1"
    assert job.payload == {
        "mode": "trajectory_summary",
        "trial_id": "trial-1",
        "schema_version": SCHEMA_VERSION,
        "triggered_by_user_id": None,
    }
    session.add.assert_called_once_with(job)
    session.flush.assert_awaited_once()

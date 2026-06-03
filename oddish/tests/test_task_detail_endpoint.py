"""Unit tests for ``get_task_detail_core`` and friends.

Covers the three things the PR review flagged: that the N+1 fix in
``list_task_versions_core`` actually skips the redundant task fetch when
a caller already has the row, that cross-org access returns 404, and
that cost totals + per-version rollups aggregate the way the detail page
relies on.

We follow the SimpleNamespace + monkeypatch convention from
``test_task_status_versioning`` rather than spinning up Postgres.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import endpoints
from oddish.db import TrialStatus
from oddish.schemas import TaskVersionResponse


# ---------------------------------------------------------------------------
# Fake session machinery
# ---------------------------------------------------------------------------


class _ScalarsResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _Result:
    def __init__(self, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _ScalarsResult(self._scalars)


class _RecordingSession:
    """Yields canned `_Result`s in order and records the queries it saw."""

    def __init__(self, results):
        self._results = list(results)
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        if not self._results:
            raise AssertionError("unexpected extra session.execute() call")
        return self._results.pop(0)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# list_task_versions_core: N+1 fix
# ---------------------------------------------------------------------------


class _VersionRow:
    """Minimal stand-in that `TaskVersionResponse.model_validate` accepts."""

    def __init__(self, id_: str, version: int):
        self.id = id_
        self.task_id = "task-1"
        self.version = version
        self.task_path = "/tmp/demo"
        self.task_s3_key = None
        self.content_hash = None
        self.message = None
        self.created_by_user_id = None
        self.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_list_task_versions_core_skips_task_fetch_when_task_passed(monkeypatch):
    fetched = {"count": 0}

    async def fake_get_task_for_org_core(*_args, **_kwargs):
        fetched["count"] += 1
        return SimpleNamespace(id="task-1")

    monkeypatch.setattr(
        endpoints, "get_task_for_org_core", fake_get_task_for_org_core
    )

    session = _RecordingSession(
        results=[_Result(scalars=[_VersionRow("v1", 1), _VersionRow("v2", 2)])]
    )

    versions = _run(
        endpoints.list_task_versions_core(
            session,
            task_id="task-1",
            task=SimpleNamespace(id="task-1"),
        )
    )

    assert fetched["count"] == 0
    assert [(v.version, v.id) for v in versions] == [(1, "v1"), (2, "v2")]
    assert len(session.queries) == 1


def test_list_task_versions_core_fetches_task_when_not_passed(monkeypatch):
    """Regression guard: callers that don't pre-fetch still get auth-checked."""
    fetched = {"count": 0}

    async def fake_get_task_for_org_core(*_args, **_kwargs):
        fetched["count"] += 1
        return SimpleNamespace(id="task-1")

    monkeypatch.setattr(
        endpoints, "get_task_for_org_core", fake_get_task_for_org_core
    )

    session = _RecordingSession(results=[_Result(scalars=[])])

    _run(endpoints.list_task_versions_core(session, task_id="task-1"))

    assert fetched["count"] == 1


# ---------------------------------------------------------------------------
# get_task_detail_core: org scoping
# ---------------------------------------------------------------------------


def test_get_task_detail_core_cross_org_returns_404():
    """A task that doesn't match the supplied org_id must 404, not 500."""
    session = _RecordingSession(results=[_Result(scalar=None)])

    with pytest.raises(HTTPException) as exc:
        _run(
            endpoints.get_task_detail_core(
                session, task_id="task-1", org_id="other-org"
            )
        )

    assert exc.value.status_code == 404
    # Sanity check: the query actually carried the org filter.
    assert len(session.queries) == 1
    assert "org_id" in str(session.queries[0])


# ---------------------------------------------------------------------------
# get_task_detail_core: cost totals + per-version rollups
# ---------------------------------------------------------------------------


def _trial(
    *,
    id_: str,
    version_id: str,
    status,
    reward,
    cost_usd=None,
    cost_is_estimated=False,
    finished_at=None,
):
    return SimpleNamespace(
        id=id_,
        task_version_id=version_id,
        status=status,
        reward=reward,
        cost_usd=cost_usd,
        cost_is_estimated=cost_is_estimated,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=None,
        finished_at=finished_at,
    )


def test_aggregate_task_detail_rollups_totals_and_buckets():
    """Cost totals span every trial; per-version buckets capture pass/fail/partial."""
    v1_created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    v2_created = datetime(2026, 2, 1, tzinfo=timezone.utc)
    finished_late = datetime(2026, 3, 1, tzinfo=timezone.utc)

    version_rows = [
        TaskVersionResponse(
            id="v1",
            task_id="task-1",
            version=1,
            task_path="/tmp/demo",
            created_at=v1_created,
        ),
        TaskVersionResponse(
            id="v2",
            task_id="task-1",
            version=2,
            task_path="/tmp/demo",
            created_at=v2_created,
        ),
    ]

    trials = [
        # v1: clean pass + clean fail (both ran to completion).
        _trial(
            id_="t1",
            version_id="v1",
            status=TrialStatus.SUCCESS,
            reward=1.0,
            cost_usd=0.10,
            cost_is_estimated=False,
        ),
        _trial(
            id_="t2",
            version_id="v1",
            status=TrialStatus.SUCCESS,
            reward=0.0,
            cost_usd=0.20,
            cost_is_estimated=True,
        ),
        # v2: partial credit, harness failure, plus an in-flight pending.
        _trial(
            id_="t3",
            version_id="v2",
            status=TrialStatus.SUCCESS,
            reward=0.5,
            cost_usd=0.30,
            cost_is_estimated=False,
            finished_at=finished_late,
        ),
        _trial(
            id_="t4",
            version_id="v2",
            status=TrialStatus.FAILED,
            reward=None,
            cost_usd=None,
        ),
        _trial(
            id_="t5",
            version_id="v2",
            status=TrialStatus.RUNNING,
            reward=None,
            cost_usd=None,
        ),
    ]

    totals, versions = endpoints._aggregate_task_detail_rollups(
        trials=trials,
        version_rows=version_rows,
        current_version_id="v2",
    )

    # Cost rollup across every trial (only three carry a cost).
    assert totals.total_trials == 5
    assert totals.cost_trial_count == 3
    assert totals.cost_usd == pytest.approx(0.60)
    assert totals.cost_has_native is True
    assert totals.cost_has_estimated is True

    # Versions come back newest-first; current flag matches current_version_id.
    assert [v.version for v in versions] == [2, 1]
    v2_summary, v1_summary = versions[0], versions[1]
    assert v2_summary.is_current is True
    assert v1_summary.is_current is False

    # v1: both trials succeeded, one full pass + one zero-reward fail.
    assert v1_summary.trial_count == 2
    assert v1_summary.completed_count == 2
    assert v1_summary.failed_count == 0
    assert v1_summary.pass_count == 1
    assert v1_summary.fail_count == 1
    assert v1_summary.partial_count == 0
    assert v1_summary.reward_sum == pytest.approx(1.0)
    assert v1_summary.reward_total == 2
    assert v1_summary.cost_usd == pytest.approx(0.30)
    assert v1_summary.cost_has_native is True
    assert v1_summary.cost_has_estimated is True

    # v2: 1 partial-credit success, 1 hard failure, 1 still running.
    assert v2_summary.trial_count == 3
    assert v2_summary.completed_count == 1
    assert v2_summary.failed_count == 1
    assert v2_summary.pending_count == 1
    assert v2_summary.partial_count == 1
    assert v2_summary.pass_count == 0
    assert v2_summary.fail_count == 0
    assert v2_summary.reward_sum == pytest.approx(0.5)
    assert v2_summary.reward_total == 1
    assert v2_summary.cost_usd == pytest.approx(0.30)
    assert v2_summary.last_run_at == finished_late


def test_aggregate_task_detail_rollups_skips_orphan_version_ids():
    """Trials pointing at a version we don't have don't crash; they just
    contribute to the task-wide totals but no per-version bucket."""
    version_rows = [
        TaskVersionResponse(
            id="v1",
            task_id="task-1",
            version=1,
            task_path="/tmp/demo",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    ]
    trials = [
        _trial(
            id_="t1",
            version_id="v-deleted",
            status=TrialStatus.SUCCESS,
            reward=1.0,
            cost_usd=0.05,
        ),
        _trial(
            id_="t2",
            version_id="v1",
            status=TrialStatus.SUCCESS,
            reward=1.0,
            cost_usd=0.05,
        ),
    ]

    totals, versions = endpoints._aggregate_task_detail_rollups(
        trials=trials,
        version_rows=version_rows,
        current_version_id="v1",
    )

    assert totals.total_trials == 2
    assert totals.cost_usd == pytest.approx(0.10)
    assert len(versions) == 1
    # Only the trial whose version_id is present gets bucketed.
    assert versions[0].trial_count == 1

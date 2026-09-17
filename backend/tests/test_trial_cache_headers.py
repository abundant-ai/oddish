"""Cache policy for single-trial reads: finished trials may be held, live ones not."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import Request, Response

from api.routers.trials import get_trial_full, get_trial_trajectory
from api.trial_cache import (
    FINISHED_TRIAL_CACHE_CONTROL,
    LIVE_TRIAL_CACHE_CONTROL,
    matches_if_none_match,
    trial_detail_is_final,
    trial_etag,
    trial_execution_is_final,
)
from oddish.db.models import AnalysisStatus, TrialStatus

FINISHED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "method": "GET"})


def test_execution_final_requires_terminal_status_and_finished_at():
    assert trial_execution_is_final(status=TrialStatus.SUCCESS, finished_at=FINISHED)
    assert trial_execution_is_final(status="failed", finished_at=FINISHED)
    assert trial_execution_is_final(status=TrialStatus.SKIPPED, finished_at=FINISHED)
    assert not trial_execution_is_final(
        status=TrialStatus.RUNNING, finished_at=FINISHED
    )
    assert not trial_execution_is_final(
        status=TrialStatus.RETRYING, finished_at=FINISHED
    )
    assert not trial_execution_is_final(status=TrialStatus.SUCCESS, finished_at=None)


def test_detail_final_also_requires_terminal_analysis():
    base = dict(status=TrialStatus.SUCCESS, finished_at=FINISHED)
    assert trial_detail_is_final(**base, analysis_status=AnalysisStatus.SUCCESS)
    assert trial_detail_is_final(**base, analysis_status="failed")
    # No analysis yet: a task-level QA run may still write one.
    assert not trial_detail_is_final(**base, analysis_status=None)
    assert not trial_detail_is_final(**base, analysis_status=AnalysisStatus.QUEUED)
    assert not trial_detail_is_final(**base, analysis_status=AnalysisStatus.RUNNING)


def test_etag_changes_with_attempt_and_analysis_time():
    a = trial_etag(trial_id="t1", attempts=1, finished_at=FINISHED)
    assert a.startswith('W/"') and a.endswith('"')
    assert a == trial_etag(trial_id="t1", attempts=1, finished_at=FINISHED)
    assert a != trial_etag(trial_id="t1", attempts=2, finished_at=FINISHED)
    assert a != trial_etag(
        trial_id="t1", attempts=1, finished_at=FINISHED, analysis_finished_at=FINISHED
    )
    assert matches_if_none_match(f"x, {a}", a)
    assert matches_if_none_match("*", a)
    assert not matches_if_none_match(None, a)
    assert not matches_if_none_match('W/"other"', a)


def _detail(**overrides):
    fields = dict(
        id="t1",
        attempts=1,
        status=TrialStatus.SUCCESS,
        finished_at=FINISHED,
        analysis_status=AnalysisStatus.SUCCESS,
        analysis_finished_at=FINISHED,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Session:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (_detail(), FINISHED_TRIAL_CACHE_CONTROL),
        (_detail(analysis_status=AnalysisStatus.QUEUED), LIVE_TRIAL_CACHE_CONTROL),
        (
            _detail(status=TrialStatus.RUNNING, finished_at=None),
            LIVE_TRIAL_CACHE_CONTROL,
        ),
        (_detail(analysis_status=None), LIVE_TRIAL_CACHE_CONTROL),
    ],
)
async def test_trial_detail_sets_cache_policy_from_row_state(detail, expected):
    response = Response()
    auth = SimpleNamespace(require_scope=Mock(), org_id="org")
    with (
        patch("api.routers.trials.authorized_read_session", return_value=_Session()),
        patch(
            "api.routers.trials.get_trial_response_for_org_core",
            new=AsyncMock(return_value=detail),
        ),
    ):
        result = await get_trial_full(_request(), response, "t1", auth)

    assert result is detail
    assert response.headers["cache-control"] == expected
    assert response.headers["etag"] == trial_etag(
        trial_id="t1",
        attempts=detail.attempts,
        finished_at=detail.finished_at,
        analysis_finished_at=detail.analysis_finished_at,
    )


@pytest.mark.asyncio
async def test_finished_trajectory_is_cacheable_and_answers_304_before_reading():
    trial = SimpleNamespace(
        id="t1",
        attempts=1,
        status=TrialStatus.SUCCESS,
        finished_at=FINISHED,
        has_trajectory=True,
    )
    auth = SimpleNamespace(require_scope=Mock())
    etag = trial_etag(trial_id="t1", attempts=1, finished_at=FINISHED)
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.routers.trials.read_trial_trajectory",
            new=AsyncMock(return_value={"steps": []}),
        ) as read,
    ):
        full = await get_trial_trajectory(_request(), "t1", auth)
        assert full.status_code == 200
        assert full.body == b'{"steps":[]}'
        assert full.headers["cache-control"] == FINISHED_TRIAL_CACHE_CONTROL
        assert full.headers["etag"] == etag
        read.assert_awaited_once()

        cached = await get_trial_trajectory(
            _request({"If-None-Match": etag}), "t1", auth
        )
        assert cached.status_code == 304
        assert cached.headers["etag"] == etag
        read.assert_awaited_once()  # the 304 never touched storage


@pytest.mark.asyncio
async def test_running_trajectory_is_no_store_and_ignores_if_none_match():
    trial = SimpleNamespace(
        id="t1",
        attempts=1,
        status=TrialStatus.RUNNING,
        finished_at=None,
        has_trajectory=False,
    )
    auth = SimpleNamespace(require_scope=Mock())
    etag = trial_etag(trial_id="t1", attempts=1, finished_at=None)
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.routers.trials.read_trial_trajectory",
            new=AsyncMock(return_value=None),
        ) as read,
    ):
        result = await get_trial_trajectory(
            _request({"If-None-Match": etag}), "t1", auth
        )

    assert result.status_code == 200
    assert result.body == b"null"
    assert result.headers["cache-control"] == LIVE_TRIAL_CACHE_CONTROL
    read.assert_awaited_once()


@pytest.mark.asyncio
async def test_finished_trial_with_missing_trajectory_is_not_cached_or_304d():
    """A storage miss on a finished trial must not be pinned for a day."""
    trial = SimpleNamespace(
        id="t1",
        attempts=1,
        status=TrialStatus.SUCCESS,
        finished_at=FINISHED,
        has_trajectory=True,
    )
    auth = SimpleNamespace(require_scope=Mock())
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.routers.trials.read_trial_trajectory",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await get_trial_trajectory(_request(), "t1", auth)
    assert result.status_code == 200
    assert result.body == b"null"
    assert result.headers["cache-control"] == LIVE_TRIAL_CACHE_CONTROL


@pytest.mark.asyncio
async def test_row_without_recorded_trajectory_never_answers_304():
    """The validator alone is not proof a body exists; the row must say so."""
    trial = SimpleNamespace(
        id="t1",
        attempts=1,
        status=TrialStatus.SUCCESS,
        finished_at=FINISHED,
        has_trajectory=False,
    )
    auth = SimpleNamespace(require_scope=Mock())
    etag = trial_etag(trial_id="t1", attempts=1, finished_at=FINISHED)
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.routers.trials.read_trial_trajectory",
            new=AsyncMock(return_value=None),
        ) as read,
    ):
        result = await get_trial_trajectory(
            _request({"If-None-Match": etag}), "t1", auth
        )
    assert result.status_code == 200
    assert result.headers["cache-control"] == LIVE_TRIAL_CACHE_CONTROL
    read.assert_awaited_once()

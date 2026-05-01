"""Tests for trajectory_summary read/write helpers in oddish.core.trial_io."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _trial(trial_id: str = "t1", *, finished: bool = True) -> SimpleNamespace:
    """Lightweight TrialModel double — only the attributes trial_io reads."""
    return SimpleNamespace(
        id=trial_id,
        name="trial-0",
        trial_s3_key=f"trials/{trial_id}/",
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc) if finished else None,
    )


def _fake_storage_with_text(text: str | None) -> MagicMock:
    storage = MagicMock()
    if text is None:
        storage.download_text = AsyncMock(side_effect=Exception("no key"))
    else:
        storage.download_text = AsyncMock(return_value=text)
    storage.upload_bytes = AsyncMock()
    storage.list_keys = AsyncMock(return_value=[])
    return storage


@pytest.mark.asyncio
async def test_read_returns_existing_summary_from_s3():
    from oddish.core.trial_io import (
        _TRAJECTORY_SUMMARY_CACHE,
        read_trial_trajectory_summary,
    )

    _TRAJECTORY_SUMMARY_CACHE.clear()
    payload = {"schema_version": "1", "summary": "x", "highlights": []}
    storage = _fake_storage_with_text(json.dumps(payload))

    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_trajectory_summary(_trial("t-existing"))
    assert result == payload


@pytest.mark.asyncio
async def test_read_returns_none_when_no_trajectory_to_summarize():
    from oddish.core.trial_io import (
        _TRAJECTORY_SUMMARY_CACHE,
        read_trial_trajectory_summary,
    )

    _TRAJECTORY_SUMMARY_CACHE.clear()
    storage = _fake_storage_with_text(None)

    with patch("oddish.core.trial_io.get_storage_client", return_value=storage), patch(
        "oddish.core.trial_io._read_trial_trajectory_uncached",
        new=AsyncMock(return_value=None),
    ):
        result = await read_trial_trajectory_summary(_trial("t-no-traj"))
    assert result is None


@pytest.mark.asyncio
async def test_read_lazily_generates_writes_and_caches():
    from oddish.core.trial_io import (
        _TRAJECTORY_SUMMARY_CACHE,
        read_trial_trajectory_summary,
    )

    _TRAJECTORY_SUMMARY_CACHE.clear()
    storage = _fake_storage_with_text(None)
    fake_trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [{"step_id": 1, "timestamp": None, "source": "agent",
                   "model_name": None, "message": "ok",
                   "reasoning_content": None, "tool_calls": None,
                   "observation": None, "metrics": None}],
        "notes": None,
        "final_metrics": None,
    }
    fake_summary = {
        "schema_version": "1",
        "model": "claude-sonnet-4-6",
        "generated_at": "2026-04-30T12:00:00+00:00",
        "summary": "ran one step",
        "highlights": [],
    }

    with patch("oddish.core.trial_io.get_storage_client", return_value=storage), patch(
        "oddish.core.trial_io._read_trial_trajectory_uncached",
        new=AsyncMock(return_value=fake_trajectory),
    ), patch(
        "api.services.summarize_trajectory.generate",
        new=AsyncMock(return_value=fake_summary),
    ) as mock_gen:
        result = await read_trial_trajectory_summary(_trial("t-gen"))

    assert result == fake_summary
    assert mock_gen.await_count == 1
    storage.upload_bytes.assert_awaited_once()
    args, kwargs = storage.upload_bytes.await_args
    written_bytes, written_key = args[0], args[1]
    assert written_key == "trials/t-gen/agent/trajectory_summary.json"
    assert json.loads(written_bytes.decode("utf-8")) == fake_summary

    # Second call should hit the in-memory cache and not re-generate or re-write.
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage), patch(
        "api.services.summarize_trajectory.generate",
        new=AsyncMock(return_value=fake_summary),
    ) as mock_gen2:
        result2 = await read_trial_trajectory_summary(_trial("t-gen"))
    assert result2 == fake_summary
    assert mock_gen2.await_count == 0


@pytest.mark.asyncio
async def test_write_failure_after_generate_still_returns_summary():
    """Best-effort persistence: if S3 write fails, return the generated summary
    so the user isn't shown an error for a successfully-generated summary."""
    from oddish.core.trial_io import (
        _TRAJECTORY_SUMMARY_CACHE,
        read_trial_trajectory_summary,
    )

    _TRAJECTORY_SUMMARY_CACHE.clear()
    storage = _fake_storage_with_text(None)
    storage.upload_bytes = AsyncMock(side_effect=RuntimeError("S3 down"))
    fake_trajectory = {"steps": [{"step_id": 1}], "schema_version": "0.1",
                       "session_id": "s", "agent": {"name": "x", "version": "1", "model_name": None},
                       "notes": None, "final_metrics": None}
    fake_summary = {"schema_version": "1", "model": "claude-sonnet-4-6",
                    "generated_at": "now", "summary": "x", "highlights": []}

    with patch("oddish.core.trial_io.get_storage_client", return_value=storage), patch(
        "oddish.core.trial_io._read_trial_trajectory_uncached",
        new=AsyncMock(return_value=fake_trajectory),
    ), patch(
        "api.services.summarize_trajectory.generate",
        new=AsyncMock(return_value=fake_summary),
    ):
        result = await read_trial_trajectory_summary(_trial("t-s3-fail"))
    assert result == fake_summary

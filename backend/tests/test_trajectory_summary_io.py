"""Compatibility coverage for pre-manifest trajectory-summary artifacts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _trial(trial_id: str = "t-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=trial_id,
        name="",
        model="anthropic/claude-test",
        trial_s3_key=f"trials/{trial_id}/",
        harbor_result_path=None,
    )


def _legacy_storage(available: dict[str, str]) -> MagicMock:
    storage = MagicMock()

    async def download(key: str) -> str:
        if key in available:
            return available[key]
        raise FileNotFoundError(key)

    storage.object_exists = AsyncMock(return_value=False)
    storage.download_text = AsyncMock(side_effect=download)
    storage.list_keys = AsyncMock(return_value=[])
    return storage


@pytest.mark.asyncio
async def test_summary_inputs_read_manifestless_root_artifacts():
    from oddish.core.trial_io import read_trial_summary_inputs

    storage = _legacy_storage(
        {
            "trials/t-1/task/instruction.md": "Solve the problem.",
            "trials/t-1/verifier/test-stdout.txt": "PASS\n",
        }
    )
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_summary_inputs(_trial())

    assert result == (None, "Solve the problem.", "PASS\n")


@pytest.mark.asyncio
async def test_summary_inputs_fall_back_to_manifestless_trial_zero_directory():
    from oddish.core.trial_io import read_trial_summary_inputs

    storage = _legacy_storage(
        {
            "trials/t-1/trial-0/task/instruction.md": "Do the thing.",
            "trials/t-1/trial-0/verifier/test-stdout.txt": "PASS\n",
        }
    )
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_summary_inputs(_trial())

    assert result == (None, "Do the thing.", "PASS\n")

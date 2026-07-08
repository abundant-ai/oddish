"""Tests for the small S3 helpers used by the trajectory-summary prompt builder.

Covers ``read_trial_instruction`` and ``read_trial_verifier_output``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _trial(trial_id: str = "t-1") -> SimpleNamespace:
    """Lightweight TrialModel double — only attributes trial_io reads."""
    return SimpleNamespace(
        id=trial_id,
        name="trial-0",
        trial_s3_key=f"trials/{trial_id}/",
    )


def _fake_storage_with_text(text: str | None) -> MagicMock:
    storage = MagicMock()
    if text is None:
        storage.download_text = AsyncMock(side_effect=FileNotFoundError())
    else:
        storage.download_text = AsyncMock(return_value=text)
    return storage


def _fake_storage_serving_keys(available: dict[str, str]) -> MagicMock:
    """Storage double that returns text only for keys present in ``available``."""
    storage = MagicMock()

    async def _download(key: str) -> str:
        if key in available:
            return available[key]
        raise FileNotFoundError(key)

    storage.download_text = AsyncMock(side_effect=_download)
    return storage


@pytest.mark.asyncio
async def test_read_trial_instruction_returns_text_when_present():
    from oddish.core.trial_io import read_trial_instruction

    storage = _fake_storage_with_text("Solve the problem.")
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_instruction(_trial())
    assert result == "Solve the problem."


@pytest.mark.asyncio
async def test_read_trial_instruction_returns_none_on_missing_key():
    from oddish.core.trial_io import read_trial_instruction

    storage = _fake_storage_with_text(None)
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_instruction(_trial())
    assert result is None


@pytest.mark.asyncio
async def test_read_trial_verifier_output_prefers_test_stdout():
    from oddish.core.trial_io import read_trial_verifier_output

    storage = _fake_storage_with_text("PASS\n")
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_verifier_output(_trial())
    assert result == "PASS\n"


@pytest.mark.asyncio
async def test_read_trial_verifier_output_returns_none_when_missing():
    from oddish.core.trial_io import read_trial_verifier_output

    storage = _fake_storage_with_text(None)
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_verifier_output(_trial())
    assert result is None


@pytest.mark.asyncio
async def test_read_trial_instruction_falls_back_to_trial_0_dir():
    """Harbor layouts stage artifacts under `trial-0/` even when the trial's
    name is empty; the reader must still find them."""
    from oddish.core.trial_io import read_trial_instruction

    trial = SimpleNamespace(id="t-1", name="", trial_s3_key="trials/t-1/")
    storage = _fake_storage_serving_keys(
        {"trials/t-1/trial-0/task/instruction.md": "Do the thing."}
    )
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_instruction(trial)
    assert result == "Do the thing."


@pytest.mark.asyncio
async def test_read_trial_verifier_output_falls_back_to_trial_0_dir():
    from oddish.core.trial_io import read_trial_verifier_output

    trial = SimpleNamespace(id="t-1", name="", trial_s3_key="trials/t-1/")
    storage = _fake_storage_serving_keys(
        {"trials/t-1/trial-0/verifier/test-stdout.txt": "PASS\n"}
    )
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_verifier_output(trial)
    assert result == "PASS\n"

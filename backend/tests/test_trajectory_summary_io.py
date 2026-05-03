"""Tests for the small S3 helpers used by the trajectory-summary prompt builder.

(D1 adds read_trial_instruction; D2 will add read_trial_verifier_output and
extend this file.)
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

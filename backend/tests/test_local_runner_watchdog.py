"""Unit tests for the in-container CPU watchdog dispatch logic.

These exercise ``_find_trial_container`` only -- the actual ``docker exec``
that injects ``_WATCHDOG_SCRIPT`` is mocked away because we don't want a
real Docker daemon in unit tests. The integration path (starts in
``_run_harbor_trial``) is covered indirectly by ``test_local_runner.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from oddish.worker.local_runner import _find_trial_container


@pytest.mark.asyncio
async def test_find_trial_container_matches_suffix():
    """Should match a container whose name ends with ``<trial>-main-1``."""
    with patch(
        "oddish.worker.local_runner.asyncio.create_subprocess_exec"
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(
                b"odd-pg\nodd-s3\nrust-c-compiler__abc-main-1\n",
                b"",
            )
        )
        mock_exec.return_value = proc

        result = await _find_trial_container("rust-c-compiler__abc", timeout=1.0)

    assert result == "rust-c-compiler__abc-main-1"


@pytest.mark.asyncio
async def test_find_trial_container_returns_none_on_timeout():
    """Should return None when the container never appears within the timeout."""
    with patch(
        "oddish.worker.local_runner.asyncio.create_subprocess_exec"
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"odd-pg\n", b""))
        mock_exec.return_value = proc

        result = await _find_trial_container("nonexistent-trial", timeout=0.5)

    assert result is None


@pytest.mark.asyncio
async def test_find_trial_container_case_insensitive():
    """Container-name matching should be case-insensitive on the suffix."""
    with patch(
        "oddish.worker.local_runner.asyncio.create_subprocess_exec"
    ) as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(b"My-Project-MyTask__XYZ-main-1\n", b"")
        )
        mock_exec.return_value = proc

        result = await _find_trial_container("MyTask__XYZ", timeout=1.0)

    assert result == "My-Project-MyTask__XYZ-main-1"

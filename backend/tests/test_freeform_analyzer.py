"""Tests for ``worker.local_runner._run_freeform_analyzer``.

The analyzer makes a single Claude API call and parses a JSON response.
We patch ``anthropic.AsyncAnthropic`` so the test runs offline.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oddish.worker.local_runner import _run_freeform_analyzer


@pytest.mark.asyncio
async def test_freeform_analyzer_parses_json_response():
    """Happy path: model returns clean JSON, analyzer fills the dict."""
    fake_response = MagicMock()
    fake_response.content = [
        MagicMock(
            text=(
                '{"headline":"agent enumerated cheats",'
                '"summary":"...",'
                '"key_actions":["read tests"],'
                '"cheating_attempted":false,'
                '"cheating_succeeded":null,'
                '"evidence":"transcript step 4"}'
            )
        )
    ]
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        result = await _run_freeform_analyzer(
            extra_instructions="find cheats",
            agent_messages=[
                {"kind": "assistant_text", "text": "Looking at tests..."}
            ],
            verifier_stdout="passed: 0",
            reward=0.0,
        )

    assert result["kind"] == "freeform_summary"
    assert result["headline"] == "agent enumerated cheats"
    assert result["cheating_attempted"] is False
    assert result["cheating_succeeded"] is None
    assert result["model"] == "claude-sonnet-4-6"

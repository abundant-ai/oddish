from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.routers.trials import get_trial_logs_structured


@pytest.mark.asyncio
async def test_structured_logs_forwards_verifier_only_to_reader():
    auth = SimpleNamespace(require_scope=Mock())
    trial = SimpleNamespace(id="trial-1")
    response = {
        "trial_id": "trial-1",
        "verifier": {"stdout": "PASS\n", "stderr": None},
        "exception": None,
    }
    with (
        patch(
            "api.routers.trials._get_authorized_trial",
            new=AsyncMock(return_value=trial),
        ),
        patch(
            "api.routers.trials.read_trial_logs_structured",
            new=AsyncMock(return_value=response),
        ) as read_logs,
    ):
        result = await get_trial_logs_structured("trial-1", auth, verifier_only=True)

    assert result == response
    read_logs.assert_awaited_once_with(trial, verifier_only=True)

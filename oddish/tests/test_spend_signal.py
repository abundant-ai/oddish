"""The spend signal reaches only a backend that can act on it, and never fails
a trial when none can."""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oddish.workers.harbor import spend_signal


class _EnvType(Enum):
    NUMINOUS = "numinous"
    DOCKER = "docker"


class _HarborEnv:
    """A live harbor environment knows its own type via a static method."""

    @staticmethod
    def type():
        return _EnvType.NUMINOUS


def test_provider_name_from_every_shape_the_callers_have() -> None:
    assert spend_signal._provider_name(_EnvType.NUMINOUS) == "numinous"
    assert spend_signal._provider_name("numinous") == "numinous"
    assert spend_signal._provider_name(_HarborEnv()) == "numinous"
    assert spend_signal._provider_name(None) is None
    assert spend_signal._provider_name(object()) is None


@pytest.mark.asyncio
async def test_signal_reaches_a_backend_that_implements_it(monkeypatch) -> None:
    backend = SimpleNamespace(report_spend=AsyncMock(return_value=True))
    monkeypatch.setattr(
        "oddish.runtime.registry.get_backend",
        lambda name: backend if name == "numinous" else None,
    )
    ok = await spend_signal.signal_spend(
        _EnvType.NUMINOUS,
        "trial-1",
        kind="usage",
        cumulative_usd=2.5,
        input_tokens=10,
        output_tokens=5,
        note={"attempt": 1},
    )
    assert ok is True
    backend.report_spend.assert_awaited_once_with(
        "trial-1",
        kind="usage",
        cumulative_usd=2.5,
        input_tokens=10,
        output_tokens=5,
        note={"attempt": 1},
    )


@pytest.mark.asyncio
async def test_a_backend_without_the_method_is_skipped(monkeypatch) -> None:
    """Modal, Docker and the rest have no report_spend. They must cost nothing."""
    monkeypatch.setattr(
        "oddish.runtime.registry.get_backend", lambda name: SimpleNamespace()
    )
    assert (
        await spend_signal.signal_spend(_EnvType.DOCKER, "trial-1", kind="usage")
        is False
    )


@pytest.mark.asyncio
async def test_a_raising_backend_does_not_fail_the_trial(monkeypatch) -> None:
    backend = SimpleNamespace(report_spend=AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr("oddish.runtime.registry.get_backend", lambda name: backend)
    assert (
        await spend_signal.signal_spend(_EnvType.NUMINOUS, "trial-1", kind="usage")
        is False
    )


@pytest.mark.asyncio
async def test_no_trial_id_no_call(monkeypatch) -> None:
    backend = SimpleNamespace(report_spend=AsyncMock(return_value=True))
    monkeypatch.setattr("oddish.runtime.registry.get_backend", lambda name: backend)
    assert await spend_signal.signal_spend(_EnvType.NUMINOUS, "", kind="usage") is False
    backend.report_spend.assert_not_awaited()

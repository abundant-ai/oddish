from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from oddish.runtime.backends.thunder import ThunderBackend


def test_capabilities_match_thunder_sdk_030() -> None:
    caps = ThunderBackend().capabilities()

    assert caps.gpu is not None
    assert caps.gpu.accelerators == ("A6000", "A100", "H100")
    assert caps.gpu.max_count == 8
    assert caps.network_egress == "configurable"
    assert caps.private_registry_pull is False


def test_harbor_env_kwargs_preserve_caller_values() -> None:
    original = {"gpu_type": "H100", "provision_attempts": 1}

    result = ThunderBackend().harbor_env_kwargs(original)

    assert result == original
    assert result is not original


@pytest.mark.asyncio
async def test_teardown_terminates_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    terminated = False

    class FakeSandbox:
        async def terminate(self) -> None:
            nonlocal terminated
            terminated = True

    class FakeAsyncSandbox:
        @staticmethod
        async def from_id(external_id: str) -> FakeSandbox:
            assert external_id == "sb-123"
            return FakeSandbox()

    module = ModuleType("thunder_sandbox")
    module.AsyncSandbox = FakeAsyncSandbox  # type: ignore[attr-defined]
    module.NotFoundError = type("NotFoundError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "thunder_sandbox", module)

    assert await ThunderBackend().teardown("sb-123") is True
    assert terminated is True


@pytest.mark.asyncio
async def test_teardown_treats_missing_sandbox_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_found = type("NotFoundError", (Exception,), {})

    class FakeAsyncSandbox:
        @staticmethod
        async def from_id(external_id: str):
            raise not_found(external_id)

    module = ModuleType("thunder_sandbox")
    module.AsyncSandbox = FakeAsyncSandbox  # type: ignore[attr-defined]
    module.NotFoundError = not_found  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "thunder_sandbox", module)

    assert await ThunderBackend().teardown("already-gone") is True


@pytest.mark.asyncio
async def test_teardown_converts_provider_failure_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncSandbox:
        @staticmethod
        async def from_id(external_id: str):
            raise RuntimeError(external_id)

    module = ModuleType("thunder_sandbox")
    module.AsyncSandbox = FakeAsyncSandbox  # type: ignore[attr-defined]
    module.NotFoundError = type("NotFoundError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "thunder_sandbox", module)

    assert await ThunderBackend().teardown("provider-error") is False


@pytest.mark.asyncio
async def test_teardown_rejects_empty_id_without_importing_sdk() -> None:
    assert await ThunderBackend().teardown("") is False

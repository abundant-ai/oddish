from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
from harbor.models.environment_type import EnvironmentType

from oddish.runtime.backends.thunder import ThunderBackend, ThunderSandboxSnapshot
from oddish.runtime.sandbox_lifecycle import SandboxLaunchContext
from oddish.schemas import HarborConfig
from oddish.workers.harbor.runner import _resolve_provider_environment_config


def test_capabilities_match_thunder_sdk_041() -> None:
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


def test_runner_forces_inventory_safe_thunder_sandbox_name() -> None:
    context = SandboxLaunchContext(
        sandbox_run_id="sandbox-run-456",
        worker_job_id="worker-job-1",
        worker_job_attempt=1,
        trial_id="trial-1",
        launch_token="launch-token",
        deployment="thunder",
        aws_account_id="",
        region="",
        provider="thunder",
    )
    config = HarborConfig(
        environment={"kwargs": {"sandbox_name": "task-controlled"}}
    )

    resolved = _resolve_provider_environment_config(
        hc=config,
        environment=EnvironmentType.THUNDER,
        backend=ThunderBackend(),
        is_probe=False,
        trial_id="trial-1",
        worker_job_id="worker-job-1",
        sandbox_launch=context,
    )

    assert resolved.kwargs["sandbox_name"] == "sandbox-run-456"


@pytest.mark.asyncio
async def test_teardown_terminates_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    terminated = False

    class FakeSandbox:
        async def terminate_async(self) -> None:
            nonlocal terminated
            terminated = True

    class FakeSandboxType:
        @staticmethod
        async def from_id_async(external_id: str) -> FakeSandbox:
            assert external_id == "sb-123"
            return FakeSandbox()

    module = ModuleType("thunder_sandbox")
    module.Sandbox = FakeSandboxType  # type: ignore[attr-defined]
    module.NotFoundError = type("NotFoundError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "thunder_sandbox", module)

    assert await ThunderBackend().teardown("sb-123") is True
    assert terminated is True


@pytest.mark.asyncio
async def test_teardown_treats_missing_sandbox_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_found = type("NotFoundError", (Exception,), {})

    class FakeSandboxType:
        @staticmethod
        async def from_id_async(external_id: str):
            raise not_found(external_id)

    module = ModuleType("thunder_sandbox")
    module.Sandbox = FakeSandboxType  # type: ignore[attr-defined]
    module.NotFoundError = not_found  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "thunder_sandbox", module)

    assert await ThunderBackend().teardown("already-gone") is True


@pytest.mark.asyncio
async def test_teardown_converts_provider_failure_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSandboxType:
        @staticmethod
        async def from_id_async(external_id: str):
            raise RuntimeError(external_id)

    module = ModuleType("thunder_sandbox")
    module.Sandbox = FakeSandboxType  # type: ignore[attr-defined]
    module.NotFoundError = type("NotFoundError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "thunder_sandbox", module)

    assert await ThunderBackend().teardown("provider-error") is False


@pytest.mark.asyncio
async def test_teardown_rejects_empty_id_without_importing_sdk() -> None:
    assert await ThunderBackend().teardown("") is False


@pytest.mark.asyncio
async def test_inventory_returns_provider_id_and_ownership_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSandbox:
        id = "sb-123"
        name = "sandbox-run-456"

    class FakeClient:
        @classmethod
        def from_cli(cls):
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def list_sandboxes_async(self, *, status: str):
            assert status == "active"
            yield FakeSandbox()

    module = ModuleType("thunder_sandbox")
    module.Client = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "thunder_sandbox", module)

    assert await ThunderBackend().snapshot_sandboxes_direct() == (
        ThunderSandboxSnapshot(external_id="sb-123", name="sandbox-run-456"),
    )

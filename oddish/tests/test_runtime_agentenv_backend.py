from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.runtime.backends.agentenv import AgentenvBackend


def test_agentenv_backend_uses_harbor_e2b_environment_name() -> None:
    assert AgentenvBackend().name == "e2b"


def test_agentenv_capabilities_advertise_snapshot_fork() -> None:
    caps = AgentenvBackend().capabilities()
    assert caps.gpu is None
    assert caps.memory_snapshot_fork is True
    assert caps.streaming_logs is True
    assert caps.private_registry_pull is False


def test_agentenv_env_kwargs_preserves_caller_values(monkeypatch) -> None:
    monkeypatch.setattr(
        "oddish.runtime.backends.agentenv.settings.agentenv_api_url",
        "http://agentenv.test:8000",
    )
    monkeypatch.setattr(
        "oddish.runtime.backends.agentenv.settings.agentenv_sandbox_url",
        None,
    )
    monkeypatch.setattr(
        "oddish.runtime.backends.agentenv.settings.agentenv_api_key",
        "dummy",
    )
    monkeypatch.delenv("E2B_API_URL", raising=False)
    monkeypatch.delenv("E2B_SANDBOX_URL", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    merged = AgentenvBackend().harbor_env_kwargs({"keep": "value"})
    assert merged == {"keep": "value"}
    assert os.environ["E2B_API_URL"] == "http://agentenv.test:8000"
    assert os.environ["E2B_SANDBOX_URL"] == "http://agentenv.test:8000"


def test_agentenv_teardown_deletes_sandbox(monkeypatch) -> None:
    monkeypatch.setattr(
        "oddish.runtime.backends.agentenv.settings.agentenv_api_url",
        "http://agentenv.test:8000",
    )
    monkeypatch.setattr(
        "oddish.runtime.backends.agentenv.settings.agentenv_api_key",
        "dummy",
    )

    response = types.SimpleNamespace(raise_for_status=lambda: None)
    client = AsyncMock()
    client.delete.return_value = response
    client.__aenter__.return_value = client
    httpx = types.SimpleNamespace(AsyncClient=lambda timeout: client)
    monkeypatch.setitem(sys.modules, "httpx", httpx)

    assert asyncio.run(AgentenvBackend().teardown("sandbox-123")) is True
    client.delete.assert_awaited_once_with(
        "http://agentenv.test:8000/sandboxes/sandbox-123",
        headers={"Authorization": "Bearer dummy"},
    )


def test_agentenv_capture_diagnostics_is_noop(tmp_path) -> None:
    with AgentenvBackend().capture_diagnostics(tmp_path) as result:
        assert result is None

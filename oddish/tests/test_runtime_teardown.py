from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from oddish.core.helpers import cancel_job_by_worker
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.modal import ModalBackend


def test_modal_teardown_terminates_sandbox_by_id(monkeypatch) -> None:
    calls: dict[str, str] = {}

    async def _terminate_aio() -> None:
        calls["terminated"] = "yes"

    async def _from_id_aio(external_id: str):
        calls["from_id"] = external_id
        return types.SimpleNamespace(
            terminate=types.SimpleNamespace(aio=_terminate_aio)
        )

    fake_modal = types.ModuleType("modal")
    fake_modal.Sandbox = types.SimpleNamespace(
        from_id=types.SimpleNamespace(aio=_from_id_aio)
    )
    monkeypatch.setitem(sys.modules, "modal", fake_modal)

    result = asyncio.run(ModalBackend().teardown("sb-123"))
    assert result is True
    assert calls["from_id"] == "sb-123"
    assert calls["terminated"] == "yes"


def test_modal_teardown_swallows_errors_returns_false(monkeypatch) -> None:
    async def _boom(external_id: str):
        raise RuntimeError("modal down")

    fake_modal = types.ModuleType("modal")
    fake_modal.Sandbox = types.SimpleNamespace(from_id=types.SimpleNamespace(aio=_boom))
    monkeypatch.setitem(sys.modules, "modal", fake_modal)

    assert asyncio.run(ModalBackend().teardown("sb-err")) is False


def test_daytona_teardown_gets_and_deletes_then_closes(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _FakeClient:
        async def get(self, external_id: str):
            calls["get"] = external_id
            return {"sandbox": external_id}

        async def delete(self, sandbox):
            calls["delete"] = sandbox

        async def close(self):
            calls["closed"] = True

    fake_daytona = types.ModuleType("daytona")
    fake_daytona.AsyncDaytona = lambda: _FakeClient()
    monkeypatch.setitem(sys.modules, "daytona", fake_daytona)

    result = asyncio.run(DaytonaBackend().teardown("dt-1"))
    assert result is True
    assert calls["get"] == "dt-1"
    assert calls["delete"] == {"sandbox": "dt-1"}
    assert calls["closed"] is True


def test_cancel_job_by_worker_delegates_to_modal(monkeypatch) -> None:
    seen: dict[str, str] = {}

    async def _fake_teardown(self, external_id: str) -> bool:
        seen["external_id"] = external_id
        return True

    monkeypatch.setattr(ModalBackend, "teardown", _fake_teardown)
    assert asyncio.run(cancel_job_by_worker("modal", "sb-9")) is True
    assert seen["external_id"] == "sb-9"


def test_cancel_job_by_worker_unknown_provider_returns_false() -> None:
    assert asyncio.run(cancel_job_by_worker("docker", "x")) is False
    assert asyncio.run(cancel_job_by_worker(None, "x")) is False
    assert asyncio.run(cancel_job_by_worker("modal", None)) is False

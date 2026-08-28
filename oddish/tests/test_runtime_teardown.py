from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from oddish.core.helpers import (
    cancel_job_by_worker,
    register_provider_teardown_delegate,
    unregister_provider_teardown_delegate,
)
from oddish.runtime.backends.archil import ArchilBackend
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.modal import ModalBackend


def test_archil_teardown_stops_deletes_and_closes(monkeypatch) -> None:
    calls: dict[str, object] = {}

    async def _stop_aio():
        calls["stopped"] = True
        sandbox.status = "stopped"
        return sandbox

    async def _delete_aio():
        calls["deleted"] = True

    sandbox = types.SimpleNamespace(
        status="running",
        stop=types.SimpleNamespace(aio=_stop_aio),
        delete=types.SimpleNamespace(aio=_delete_aio),
    )

    class _FakeClient:
        def __init__(self, *, timeout: int):
            calls["timeout"] = timeout

            async def _get_aio(external_id: str):
                calls["get"] = external_id
                return sandbox

            self.sandboxes = types.SimpleNamespace(
                get=types.SimpleNamespace(aio=_get_aio)
            )

            async def _close_aio():
                calls["closed"] = True

            self.close = types.SimpleNamespace(aio=_close_aio)

    fake_archil = types.ModuleType("archil")
    fake_archil.Archil = _FakeClient
    monkeypatch.setitem(sys.modules, "archil", fake_archil)

    assert asyncio.run(ArchilBackend().teardown("sb-archil")) is True
    assert calls == {
        "timeout": 120,
        "get": "sb-archil",
        "stopped": True,
        "deleted": True,
        "closed": True,
    }


def test_archil_teardown_treats_missing_sandbox_as_done(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _NotFoundError(Exception):
        status = 404

    class _FakeClient:
        def __init__(self, *, timeout: int):
            async def _get_aio(external_id: str):
                raise _NotFoundError

            self.sandboxes = types.SimpleNamespace(
                get=types.SimpleNamespace(aio=_get_aio)
            )

            async def _close_aio():
                calls["closed"] = True

            self.close = types.SimpleNamespace(aio=_close_aio)

    fake_archil = types.ModuleType("archil")
    fake_archil.Archil = _FakeClient
    monkeypatch.setitem(sys.modules, "archil", fake_archil)

    assert asyncio.run(ArchilBackend().teardown("sb-gone")) is True
    assert calls["closed"] is True


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
    fake_daytona.__path__ = []
    fake_daytona.AsyncDaytona = lambda: _FakeClient()
    fake_daytona_common = types.ModuleType("daytona.common")
    fake_daytona_common.__path__ = []
    fake_daytona_errors = types.ModuleType("daytona.common.errors")
    fake_daytona_errors.DaytonaNotFoundError = type(
        "DaytonaNotFoundError", (Exception,), {}
    )
    monkeypatch.setitem(sys.modules, "daytona", fake_daytona)
    monkeypatch.setitem(sys.modules, "daytona.common", fake_daytona_common)
    monkeypatch.setitem(sys.modules, "daytona.common.errors", fake_daytona_errors)

    result = asyncio.run(DaytonaBackend().teardown("dt-1"))
    assert result is True
    assert calls["get"] == "dt-1"
    assert calls["delete"] == {"sandbox": "dt-1"}
    assert calls["closed"] is True


def test_daytona_teardown_treats_missing_sandbox_as_done(monkeypatch, caplog) -> None:
    from daytona.common.errors import DaytonaNotFoundError

    calls: dict[str, object] = {}

    class _FakeClient:
        async def get(self, external_id: str):
            raise DaytonaNotFoundError(
                f"Sandbox with ID or name {external_id} not found",
                status_code=404,
            )

        async def close(self):
            calls["closed"] = True

    fake_daytona = types.ModuleType("daytona")
    fake_daytona.AsyncDaytona = lambda: _FakeClient()
    monkeypatch.setitem(sys.modules, "daytona", fake_daytona)

    with caplog.at_level(
        logging.INFO, logger="oddish.runtime.backends.daytona"
    ):
        assert asyncio.run(DaytonaBackend().teardown("dt-gone")) is True
    assert calls["closed"] is True
    assert "metric=daytona.sandbox_gone phase=teardown external_id=dt-gone" in (
        caplog.text
    )


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


def test_cancel_job_by_worker_uses_registered_provider_delegate_exactly_once() -> None:
    seen: list[str] = []

    async def delegate(external_id: str) -> bool:
        seen.append(external_id)
        return True

    register_provider_teardown_delegate("ec2", delegate)
    try:
        assert asyncio.run(cancel_job_by_worker("ec2", "ec2://handle")) is True
    finally:
        unregister_provider_teardown_delegate("ec2")

    assert seen == ["ec2://handle"]

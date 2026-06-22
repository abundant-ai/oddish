"""Golden test: the API cancel path stays byte-for-byte equivalent after it is
routed through ``ModalDispatcher.cancel`` instead of the hard-wired
``modal.FunctionCall`` reach-in (design spec §6.4/§10).

Asserts the same ``FunctionCall.from_id(id).cancel.aio(terminate_containers=True)``
calls happen for the same (deduped) ids, and the same count is returned.
"""

from __future__ import annotations

import asyncio
import sys
import types


def _install_fake_modal(monkeypatch) -> list[tuple[str, bool]]:
    cancelled: list[tuple[str, bool]] = []

    def _from_id(fc_id: str):
        async def _cancel_aio(*, terminate_containers: bool):
            cancelled.append((fc_id, terminate_containers))

        return types.SimpleNamespace(cancel=types.SimpleNamespace(aio=_cancel_aio))

    fake_modal = types.ModuleType("modal")
    fake_modal.FunctionCall = types.SimpleNamespace(from_id=_from_id)
    monkeypatch.setitem(sys.modules, "modal", fake_modal)
    return cancelled


def test_cancel_modal_function_calls_terminates_containers(monkeypatch) -> None:
    cancelled = _install_fake_modal(monkeypatch)
    from api.routers.tasks import _cancel_modal_function_calls

    count = asyncio.run(_cancel_modal_function_calls(["fc-1", "fc-1", "fc-2"]))

    assert count == 2
    assert sorted(cancelled) == [("fc-1", True), ("fc-2", True)]


def test_cancel_modal_function_calls_empty_returns_zero(monkeypatch) -> None:
    _install_fake_modal(monkeypatch)
    from api.routers.tasks import _cancel_modal_function_calls

    assert asyncio.run(_cancel_modal_function_calls([])) == 0


def test_cancel_modal_function_calls_swallows_errors(monkeypatch) -> None:
    def _from_id(fc_id: str):
        async def _boom(*, terminate_containers: bool):
            raise RuntimeError("modal down")

        return types.SimpleNamespace(cancel=types.SimpleNamespace(aio=_boom))

    fake_modal = types.ModuleType("modal")
    fake_modal.FunctionCall = types.SimpleNamespace(from_id=_from_id)
    monkeypatch.setitem(sys.modules, "modal", fake_modal)
    from api.routers.tasks import _cancel_modal_function_calls

    # A failing cancel is counted as not-cancelled, never raised.
    assert asyncio.run(_cancel_modal_function_calls(["fc-err"])) == 0

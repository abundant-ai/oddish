from __future__ import annotations

import asyncio
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.dispatch.backends.modal import ModalDispatcher
from oddish.dispatch.ports import WorkerHandle


class _FakeSpawnFunction:
    """Stand-in for the Modal ``process_single_job`` Function."""

    def __init__(self) -> None:
        self.calls: list[str] = []

        async def _spawn_aio(*, queue_key: str):
            self.calls.append(queue_key)
            return types.SimpleNamespace(object_id=f"fc-{len(self.calls)}")

        self.spawn = types.SimpleNamespace(aio=_spawn_aio)


def test_modal_spawn_calls_spawn_aio_per_queue_key() -> None:
    fn = _FakeSpawnFunction()
    dispatcher = ModalDispatcher(spawn_function=fn)

    async def _go():
        return list(await dispatcher.spawn(spawn_plan=["a", "a", "b"]))

    handles = asyncio.run(_go())
    # Byte-for-byte the old poll_queue behavior: one spawn.aio per plan entry.
    assert fn.calls == ["a", "a", "b"]
    assert [h.queue_key for h in handles] == ["a", "a", "b"]
    assert all(h.provider == "modal" for h in handles)


def test_modal_spawn_handles_are_provisional_with_no_id() -> None:
    # Load-bearing (spec §5.2/§11): the spawner does NOT own the id; the worker
    # self-reports it. spawn.aio's return is discarded, so handles are
    # provisional with an empty id.
    fn = _FakeSpawnFunction()
    dispatcher = ModalDispatcher(spawn_function=fn)

    async def _go():
        return list(await dispatcher.spawn(spawn_plan=["x"]))

    (handle,) = asyncio.run(_go())
    assert handle.provisional is True
    assert handle.id == ""


def test_modal_cancel_terminates_function_calls(monkeypatch) -> None:
    cancelled: list[tuple[str, bool]] = []

    def _from_id(fc_id: str):
        async def _cancel_aio(*, terminate_containers: bool):
            cancelled.append((fc_id, terminate_containers))

        return types.SimpleNamespace(cancel=types.SimpleNamespace(aio=_cancel_aio))

    fake_modal = types.ModuleType("modal")
    fake_modal.FunctionCall = types.SimpleNamespace(from_id=_from_id)
    monkeypatch.setitem(sys.modules, "modal", fake_modal)

    dispatcher = ModalDispatcher()
    handles = [
        WorkerHandle(provider="modal", queue_key="q", id="fc-1", provisional=False),
        WorkerHandle(provider="modal", queue_key="q", id="fc-1", provisional=False),
        WorkerHandle(provider="modal", queue_key="q", id="fc-2", provisional=False),
        WorkerHandle(provider="modal", queue_key="q", id="", provisional=True),
    ]

    count = asyncio.run(dispatcher.cancel(handles))
    # Dedupes fc-1, skips the empty/provisional handle, terminates containers.
    assert count == 2
    assert sorted(cancelled) == [("fc-1", True), ("fc-2", True)]


def test_modal_cancel_no_ids_returns_zero() -> None:
    dispatcher = ModalDispatcher()
    handles = [WorkerHandle(provider="modal", queue_key="q")]
    assert asyncio.run(dispatcher.cancel(handles)) == 0


def test_modal_recover_returns_durable_handle() -> None:
    dispatcher = ModalDispatcher()
    serialized = WorkerHandle(
        provider="modal", queue_key="q", id="fc-9", provisional=False
    ).serialize()
    recovered = asyncio.run(dispatcher.recover(serialized))
    assert recovered is not None
    assert recovered.id == "fc-9"


def test_modal_dispatcher_import_is_lazy() -> None:
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        f"import sys; sys.path.insert(0, {src!r})\n"
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def _guard(name, *a, **k):\n"
        "    if name == 'modal' or name.startswith('modal.'):\n"
        "        raise AssertionError('modal imported at module load: ' + name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _guard\n"
        "import oddish.dispatch.backends.modal\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"

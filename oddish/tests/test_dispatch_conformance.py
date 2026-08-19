from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

import types

from oddish.core import model_concurrency
from oddish.dispatch.backends.docker import DockerPoolDispatcher
from oddish.dispatch.backends.fake import FakeDispatcher
from oddish.dispatch.backends.inprocess import InProcessDispatcher
from oddish.dispatch.backends.k8s import K8sJobDispatcher
from oddish.dispatch.backends.modal import ModalDispatcher
from oddish.dispatch.ports import Dispatcher, WorkerHandle
from oddish.workers.queue import sandbox_capacity


class _FakeBatchApi:
    """Minimal in-memory kubernetes BatchV1Api so the k8s backend runs hermetically."""

    def __init__(self) -> None:
        self.jobs: dict[str, types.SimpleNamespace] = {}

    def create_namespaced_job(self, *, namespace, body):
        name = body["metadata"]["name"]
        self.jobs[name] = types.SimpleNamespace(status=types.SimpleNamespace(active=1))
        return self.jobs[name]

    def read_namespaced_job_status(self, *, name, namespace):
        if name not in self.jobs:
            raise RuntimeError("not found")
        return self.jobs[name]

    def delete_namespaced_job(self, *, name, namespace, **kwargs):
        if self.jobs.pop(name, None) is None:
            raise RuntimeError("not found")


class _FakeDockerCLI:
    """Minimal in-memory docker CLI so the docker backend runs hermetically."""

    def __init__(self) -> None:
        self.containers: dict[str, bool] = {}
        self._counter = 0

    async def __call__(self, args: list[str]) -> str:
        verb = args[0]
        if verb == "run":
            self._counter += 1
            cid = f"ctr{self._counter:012d}"
            self.containers[cid] = True
            return cid + "\n"
        if verb == "inspect":
            cid = args[-1]
            if cid not in self.containers:
                raise RuntimeError("No such object")
            if "-f" in args:
                return ("true" if self.containers[cid] else "false") + "\n"
            return "[{}]\n"
        if verb == "rm":
            cid = args[-1]
            if self.containers.pop(cid, None) is None:
                raise RuntimeError("No such container")
            return cid + "\n"
        raise AssertionError(f"unexpected docker verb: {verb}")


def _hermetic_modal_dispatcher() -> ModalDispatcher:
    """A ModalDispatcher wired to fakes so it runs without the Modal SDK."""

    async def _spawn_aio(*, queue_key: str):
        return None

    spawn_function = types.SimpleNamespace(spawn=types.SimpleNamespace(aio=_spawn_aio))

    async def _cancel_fn(fc_ids: list[str]) -> list[str]:
        # The cancel contract returns the ids actually cancelled; this fake
        # always succeeds, so it echoes back every id it was handed.
        return list(fc_ids)

    return ModalDispatcher(spawn_function=spawn_function, cancel_fn=_cancel_fn)


def _noop_dispatchers() -> list[Dispatcher]:
    """Dispatchers that can spawn hermetically (no real infra).

    ``InProcessDispatcher`` is given a no-op job runner so ``spawn`` creates
    tasks that complete immediately without touching the database; the Modal
    dispatcher is wired to fakes (the shared conformance suite runs against
    every backend — the fakes keep it hermetic, per spec §10).
    """

    async def _noop_run_job(queue_key: str, **_kwargs) -> bool:
        return False

    async def _noop_acquire_slot(**_kwargs) -> int:
        return 0

    async def _noop_release_slot(**_kwargs) -> None:
        return None

    return [
        FakeDispatcher(),
        InProcessDispatcher(
            run_job=_noop_run_job,
            acquire_slot=_noop_acquire_slot,
            release_slot=_noop_release_slot,
        ),
        _hermetic_modal_dispatcher(),
        DockerPoolDispatcher(image="oddish-worker:test", run_command=_FakeDockerCLI()),
        K8sJobDispatcher(image="oddish-worker:test", batch_api=_FakeBatchApi()),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancelled", "expected_events"),
    [(False, ["queue-release", "capacity-release"]), (True, ["queue-release"])],
)
async def test_inprocess_ec2_capacity_release_matches_outcome(
    monkeypatch, cancelled: bool, expected_events: list[str]
) -> None:
    events: list[str] = []

    async def _run_job(*args, **kwargs):
        if cancelled:
            raise asyncio.CancelledError
        return True

    async def _acquire_queue_slot(**kwargs):
        return 2

    async def _release_queue_slot(**kwargs):
        events.append("queue-release")

    async def _acquire_capacity(**kwargs):
        return 4

    async def _release_capacity(**kwargs):
        events.append("capacity-release")

    async def _load_limit(_queue_key):
        return 3

    monkeypatch.setattr(
        sandbox_capacity, "acquire_sandbox_capacity_lease", _acquire_capacity
    )
    monkeypatch.setattr(
        sandbox_capacity, "release_sandbox_capacity_lease", _release_capacity
    )
    monkeypatch.setattr(
        model_concurrency, "load_effective_model_concurrency_limit", _load_limit
    )
    dispatcher = InProcessDispatcher(
        run_job=_run_job,
        acquire_slot=_acquire_queue_slot,
        release_slot=_release_queue_slot,
    )

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await dispatcher._safe_run(
                "fireworks/glm-5p2",
                "worker-1",
                harbor_variant_id="default",
                execution_lane="ec2_trial",
            )
    else:
        await dispatcher._safe_run(
            "fireworks/glm-5p2",
            "worker-1",
            harbor_variant_id="default",
            execution_lane="ec2_trial",
        )

    assert events == expected_events


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_name_is_nonempty_str(dispatcher: Dispatcher) -> None:
    assert isinstance(dispatcher.name, str) and dispatcher.name


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_spawn_yields_one_handle_per_plan_entry(dispatcher: Dispatcher) -> None:
    plan = ["gpt-4o", "gpt-4o", "claude"]

    async def _go() -> list[WorkerHandle]:
        return list(await dispatcher.spawn(spawn_plan=plan))

    handles = asyncio.run(_go())
    assert len(handles) == len(plan)
    assert [h.queue_key for h in handles] == plan
    for handle in handles:
        assert handle.provider == dispatcher.name
        assert isinstance(handle.serialize(), dict)


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_spawn_empty_plan_returns_no_handles(dispatcher: Dispatcher) -> None:
    async def _go() -> list[WorkerHandle]:
        return list(await dispatcher.spawn(spawn_plan=[]))

    assert asyncio.run(_go()) == []


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_check_active_yields_only_live_handles(dispatcher: Dispatcher) -> None:
    async def _go() -> list[WorkerHandle]:
        handles = list(await dispatcher.spawn(spawn_plan=["a", "b"]))
        await dispatcher.cancel(handles)
        live = [h async for h in dispatcher.check_active(handles)]
        return live

    # After cancelling every spawned worker, none remain active. (For the
    # no-op in-process runner the tasks also finish on their own, so this is
    # robust either way.)
    assert asyncio.run(_go()) == []


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_cancel_returns_count_and_is_idempotent(dispatcher: Dispatcher) -> None:
    async def _go() -> tuple[int, int]:
        handles = list(await dispatcher.spawn(spawn_plan=["x"]))
        first = await dispatcher.cancel(handles)
        second = await dispatcher.cancel(handles)
        return first, second

    first, second = asyncio.run(_go())
    assert first >= 0
    assert second == 0  # nothing left to cancel the second time


@pytest.mark.parametrize("dispatcher", _noop_dispatchers(), ids=lambda d: d.name)
def test_recover_round_trips_a_serialized_handle(dispatcher: Dispatcher) -> None:
    async def _go() -> WorkerHandle | None:
        handle = WorkerHandle(
            provider=dispatcher.name, queue_key="qk", id="worker-1", provisional=False
        )
        return await dispatcher.recover(handle.serialize())

    recovered = asyncio.run(_go())
    # recover() may be best-effort (None) for ephemeral scalers, but when it
    # returns a handle it must round-trip the identity fields.
    if recovered is not None:
        assert recovered.provider == dispatcher.name
        assert recovered.queue_key == "qk"
        assert recovered.id == "worker-1"


def test_worker_handle_serialize_deserialize_round_trip() -> None:
    handle = WorkerHandle(
        provider="modal",
        queue_key="gpt-4o",
        id="fc-123",
        provisional=False,
        metadata={"region": "us-east"},
    )
    restored = WorkerHandle.deserialize(handle.serialize())
    assert restored == handle

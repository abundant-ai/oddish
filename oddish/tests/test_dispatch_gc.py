from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.dispatch.backends.docker import DockerPoolDispatcher
from oddish.dispatch.backends.k8s import K8sJobDispatcher
from oddish.dispatch.cycle import reclaim_leaked_workers


class _FakeDockerCLI:
    def __init__(self) -> None:
        self.containers: dict[str, bool] = {}
        self._n = 0

    async def __call__(self, args: list[str]) -> str:
        verb = args[0]
        if verb == "run":
            self._n += 1
            cid = f"ctr{self._n}"
            self.containers[cid] = True
            return cid + "\n"
        if verb == "ps":
            # ps -q --filter label=oddish_managed=1
            return "\n".join(self.containers) + "\n"
        if verb == "rm":
            self.containers.pop(args[-1], None)
            return ""
        raise AssertionError(verb)


def test_docker_list_managed_returns_running_containers() -> None:
    cli = _FakeDockerCLI()
    d = DockerPoolDispatcher(image="x", run_command=cli)

    async def _go():
        await d.spawn(spawn_plan=["a", "b"])
        return await d.list_managed()

    managed = asyncio.run(_go())
    assert {h.id for h in managed} == {"ctr1", "ctr2"}
    assert all(h.provider == "docker" for h in managed)


def test_reclaim_leaked_workers_cancels_workers_not_alive() -> None:
    cli = _FakeDockerCLI()
    d = DockerPoolDispatcher(image="x", run_command=cli)

    async def _go():
        handles = list(await d.spawn(spawn_plan=["a", "b", "c"]))
        alive = {handles[0].id}  # only the first still has a live worker_jobs row
        reclaimed = await reclaim_leaked_workers(d, alive=alive)
        still = {h.id for h in await d.list_managed()}
        return reclaimed, still

    reclaimed, still = asyncio.run(_go())
    assert reclaimed == 2
    assert len(still) == 1


def test_reclaim_noop_when_all_alive() -> None:
    cli = _FakeDockerCLI()
    d = DockerPoolDispatcher(image="x", run_command=cli)

    async def _go():
        handles = list(await d.spawn(spawn_plan=["a", "b"]))
        return await reclaim_leaked_workers(d, alive={h.id for h in handles})

    assert asyncio.run(_go()) == 0


def test_reclaim_noop_for_dispatcher_without_list_managed() -> None:
    from oddish.dispatch.backends.fake import FakeDispatcher

    async def _go():
        return await reclaim_leaked_workers(FakeDispatcher(), alive=set())

    assert asyncio.run(_go()) == 0


class _FakeBatchApi:
    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}
        self._n = 0

    def create_namespaced_job(self, *, namespace, body):
        import types

        name = body["metadata"]["name"]
        self.jobs[name] = types.SimpleNamespace(
            metadata=types.SimpleNamespace(name=name),
            status=types.SimpleNamespace(active=1),
        )
        return self.jobs[name]

    def list_namespaced_job(self, *, namespace, label_selector=None):
        import types

        return types.SimpleNamespace(items=list(self.jobs.values()))

    def delete_namespaced_job(self, *, name, namespace, **kwargs):
        if self.jobs.pop(name, None) is None:
            raise RuntimeError("not found")


def test_k8s_list_managed_returns_jobs() -> None:
    api = _FakeBatchApi()
    d = K8sJobDispatcher(image="x", namespace="oddish", batch_api=api)

    async def _go():
        await d.spawn(spawn_plan=["a", "b"])
        return await d.list_managed()

    managed = asyncio.run(_go())
    assert len(managed) == 2
    assert all(h.provider == "k8s" and h.id for h in managed)


# ---------------------------------------------------------------------------
# run_dispatch_loop wiring: leaked-worker reclaim as a low-frequency backstop
# ---------------------------------------------------------------------------
import oddish.dispatch.cycle as cycle  # noqa: E402
from oddish.dispatch.backends.fake import FakeDispatcher  # noqa: E402


def _stub_loop_deps(monkeypatch, counters: dict) -> None:
    """Stub the DB-backed cycle/reattach so the loop runs without infrastructure."""

    async def _fake_cycle(dispatcher, **kwargs):
        counters["cycles"] += 1

    async def _fake_reattach(dispatcher, **kwargs):
        return []

    monkeypatch.setattr(cycle, "run_dispatch_cycle", _fake_cycle)
    monkeypatch.setattr(cycle, "reattach_in_flight_workers", _fake_reattach)


def test_run_dispatch_loop_reclaims_every_n_cycles(monkeypatch) -> None:
    cycle._TRIGGER = None  # fresh trigger so wait() honours the tiny interval
    counters = {"cycles": 0, "reclaims": 0, "alive_seen": []}
    _stub_loop_deps(monkeypatch, counters)

    async def _fake_reclaim(dispatcher, *, alive):
        counters["reclaims"] += 1
        counters["alive_seen"].append(set(alive))
        return 0

    async def _alive():
        return {"fc-1"}

    monkeypatch.setattr(cycle, "reclaim_leaked_workers", _fake_reclaim)

    asyncio.run(
        cycle.run_dispatch_loop(
            FakeDispatcher(),
            max_workers=1,
            concurrency_for=lambda qk: 1,
            fallback_interval=0.001,
            reclaim_every_cycles=3,
            _stop=lambda: counters["cycles"] >= 6,
            _alive=_alive,
        )
    )
    # Six cycles run; reclaim fires on cycles 3 and 6 with the live handle set.
    assert counters["cycles"] == 6
    assert counters["reclaims"] == 2
    assert counters["alive_seen"] == [{"fc-1"}, {"fc-1"}]


def test_run_dispatch_loop_skips_reclaim_without_liveness_signal(monkeypatch) -> None:
    cycle._TRIGGER = None
    counters = {"cycles": 0, "reclaims": 0}
    _stub_loop_deps(monkeypatch, counters)

    async def _fake_reclaim(dispatcher, *, alive):
        counters["reclaims"] += 1
        return 0

    async def _empty_alive():
        return set()

    monkeypatch.setattr(cycle, "reclaim_leaked_workers", _fake_reclaim)

    asyncio.run(
        cycle.run_dispatch_loop(
            FakeDispatcher(),
            max_workers=1,
            concurrency_for=lambda qk: 1,
            fallback_interval=0.001,
            reclaim_every_cycles=1,
            _stop=lambda: counters["cycles"] >= 3,
            _alive=_empty_alive,
        )
    )
    # Empty alive set carries no liveness signal -> reclaim is never attempted.
    assert counters["cycles"] == 3
    assert counters["reclaims"] == 0


def test_run_dispatch_loop_swallows_reclaim_errors(monkeypatch) -> None:
    cycle._TRIGGER = None
    counters = {"cycles": 0}
    _stub_loop_deps(monkeypatch, counters)

    async def _boom_alive():
        raise RuntimeError("alive query DB down")

    asyncio.run(
        cycle.run_dispatch_loop(
            FakeDispatcher(),
            max_workers=1,
            concurrency_for=lambda qk: 1,
            fallback_interval=0.001,
            reclaim_every_cycles=1,
            _stop=lambda: counters["cycles"] >= 3,
            _alive=_boom_alive,
        )
    )
    # A raising alive-fetch is swallowed; the dispatch loop keeps ticking.
    assert counters["cycles"] == 3

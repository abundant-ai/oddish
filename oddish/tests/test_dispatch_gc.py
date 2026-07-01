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

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from oddish.dispatch.backends.k8s import K8sJobDispatcher
from oddish.dispatch.ports import WorkerHandle


class _FakeJobStatus:
    def __init__(self, active=0, succeeded=0, failed=0) -> None:
        self.active = active
        self.succeeded = succeeded
        self.failed = failed


class _FakeJob:
    def __init__(self, status: _FakeJobStatus) -> None:
        self.status = status


class _NotFound(Exception):
    pass


class FakeBatchV1Api:
    """In-memory stand-in for kubernetes.client.BatchV1Api."""

    def __init__(self) -> None:
        self.jobs: dict[str, _FakeJobStatus] = {}
        self.created: list[dict] = []

    def create_namespaced_job(self, *, namespace: str, body: dict):
        name = body["metadata"]["name"]
        self.created.append(body)
        self.jobs[name] = _FakeJobStatus(active=1)
        return _FakeJob(self.jobs[name])

    def read_namespaced_job_status(self, *, name: str, namespace: str):
        if name not in self.jobs:
            raise _NotFound(name)
        return _FakeJob(self.jobs[name])

    def delete_namespaced_job(self, *, name: str, namespace: str, **kwargs):
        if name not in self.jobs:
            raise _NotFound(name)
        del self.jobs[name]
        return None


def _dispatcher() -> tuple[K8sJobDispatcher, FakeBatchV1Api]:
    api = FakeBatchV1Api()
    dispatcher = K8sJobDispatcher(
        image="oddish-worker:test",
        namespace="oddish",
        batch_api=api,
        env={"ODDISH_DATABASE_URL": "postgres://x"},
    )
    return dispatcher, api


def test_spawn_creates_one_job_per_queue_key_with_durable_name() -> None:
    dispatcher, api = _dispatcher()

    async def _go():
        return list(await dispatcher.spawn(spawn_plan=["gpt-4o", "claude"]))

    handles = asyncio.run(_go())
    assert [h.queue_key for h in handles] == ["gpt-4o", "claude"]
    # Job names are synchronous, durable handles (no provisional ids on k8s).
    assert all(h.provider == "k8s" and h.id and not h.provisional for h in handles)
    assert len(api.created) == 2
    body = api.created[0]
    assert body["kind"] == "Job"
    spec = body["spec"]["template"]["spec"]
    assert spec["restartPolicy"] == "Never"
    container = spec["containers"][0]
    assert container["image"] == "oddish-worker:test"
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["ODDISH_WORKER_QUEUE_KEY"] == "gpt-4o"
    labels = body["metadata"]["labels"]
    assert labels["app"] == "oddish-worker"
    assert labels["oddish_managed"] == "true"


def test_spawn_sanitizes_queue_key_into_dns_safe_job_name() -> None:
    dispatcher, api = _dispatcher()

    async def _go():
        return list(await dispatcher.spawn(spawn_plan=["GPT-4o/Turbo_v2"]))

    (handle,) = asyncio.run(_go())
    # RFC1123: lowercase alphanumerics and '-', <=63 chars.
    assert handle.id == handle.id.lower()
    assert all(c.isalnum() or c == "-" for c in handle.id)
    assert len(handle.id) <= 63


def test_check_active_yields_only_running_jobs() -> None:
    dispatcher, api = _dispatcher()

    async def _go():
        handles = list(await dispatcher.spawn(spawn_plan=["a", "b"]))
        # Mark the first job complete.
        api.jobs[handles[0].id] = _FakeJobStatus(active=0, succeeded=1)
        return [h async for h in dispatcher.check_active(handles)]

    live = asyncio.run(_go())
    assert [h.queue_key for h in live] == ["b"]


def test_cancel_deletes_jobs_and_counts() -> None:
    dispatcher, api = _dispatcher()

    async def _go():
        handles = list(await dispatcher.spawn(spawn_plan=["a", "b"]))
        first = await dispatcher.cancel(handles)
        second = await dispatcher.cancel(handles)
        return first, second

    first, second = asyncio.run(_go())
    assert first == 2
    assert second == 0


def test_recover_reattaches_existing_job() -> None:
    dispatcher, api = _dispatcher()

    async def _go():
        (handle,) = list(await dispatcher.spawn(spawn_plan=["a"]))
        recovered = await dispatcher.recover(handle.serialize())
        missing = await dispatcher.recover(
            WorkerHandle(provider="k8s", queue_key="a", id="nope").serialize()
        )
        return recovered, missing

    recovered, missing = asyncio.run(_go())
    assert recovered is not None and recovered.id
    assert missing is None


def test_k8s_dispatcher_import_is_lazy() -> None:
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        f"import sys; sys.path.insert(0, {src!r})\n"
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def _guard(name, *a, **k):\n"
        "    if name in ('modal', 'kubernetes') or name.startswith(('modal.', 'kubernetes.')):\n"
        "        raise AssertionError('forbidden import at module load: ' + name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _guard\n"
        "import oddish.dispatch.backends.k8s\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Integration tests — require a real Kubernetes cluster, skipped in CI/local.
# ---------------------------------------------------------------------------
@pytest.mark.skip(reason="requires a Kubernetes cluster")
def test_integration_spawn_runs_real_job() -> None:  # pragma: no cover
    dispatcher = K8sJobDispatcher(image="oddish-worker:latest", namespace="oddish")

    async def _go():
        handles = list(await dispatcher.spawn(spawn_plan=["proof"]))
        live = [h async for h in dispatcher.check_active(handles)]
        await dispatcher.cancel(handles)
        return live

    live = asyncio.run(_go())
    assert len(live) == 1


@pytest.mark.skip(reason="requires a Kubernetes cluster")
def test_integration_recover_after_restart() -> None:  # pragma: no cover
    dispatcher = K8sJobDispatcher(image="oddish-worker:latest", namespace="oddish")

    async def _go():
        (handle,) = list(await dispatcher.spawn(spawn_plan=["proof"]))
        # A fresh dispatcher (simulating a control-plane restart) reattaches the
        # durable Job-name handle.
        fresh = K8sJobDispatcher(image="oddish-worker:latest", namespace="oddish")
        recovered = await fresh.recover(handle.serialize())
        await dispatcher.cancel([handle])
        return recovered

    assert asyncio.run(_go()) is not None

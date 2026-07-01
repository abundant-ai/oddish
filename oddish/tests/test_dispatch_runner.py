from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from oddish.dispatch.backends.docker import DockerPoolDispatcher
from oddish.dispatch.backends.inprocess import InProcessDispatcher
from oddish.dispatch.backends.k8s import K8sJobDispatcher
from oddish.dispatch.runner import build_dispatcher_from_env, worker_env_from


def test_build_inprocess_by_default() -> None:
    assert isinstance(build_dispatcher_from_env({}), InProcessDispatcher)


def test_build_docker_dispatcher() -> None:
    d = build_dispatcher_from_env(
        {"ODDISH_DISPATCHER": "docker", "ODDISH_WORKER_IMAGE": "oddish:1"}
    )
    assert isinstance(d, DockerPoolDispatcher)


def test_build_k8s_dispatcher() -> None:
    d = build_dispatcher_from_env(
        {
            "ODDISH_DISPATCHER": "k8s",
            "ODDISH_WORKER_IMAGE": "oddish:1",
            "ODDISH_K8S_NAMESPACE": "oddish",
        }
    )
    assert isinstance(d, K8sJobDispatcher)


def test_build_docker_requires_image() -> None:
    with pytest.raises(ValueError, match="ODDISH_WORKER_IMAGE"):
        build_dispatcher_from_env({"ODDISH_DISPATCHER": "docker"})


def test_build_unknown_dispatcher_raises() -> None:
    with pytest.raises(ValueError, match="unknown dispatcher"):
        build_dispatcher_from_env({"ODDISH_DISPATCHER": "nope"})


def test_worker_env_forwards_database_url_and_oddish_prefix() -> None:
    env = {
        "ODDISH_DATABASE_URL": "postgres://db",
        "ODDISH_HARBOR_ENVIRONMENT": "docker",
        "ODDISH_DISPATCHER": "docker",  # dispatcher-only, must not leak to workers
        "ODDISH_WORKER_IMAGE": "x",  # dispatcher-only
        "PATH": "/usr/bin",  # non-oddish, ignored
    }
    forwarded = worker_env_from(env)
    assert forwarded["ODDISH_DATABASE_URL"] == "postgres://db"
    assert forwarded["ODDISH_HARBOR_ENVIRONMENT"] == "docker"
    assert "ODDISH_DISPATCHER" not in forwarded
    assert "ODDISH_WORKER_IMAGE" not in forwarded
    assert "PATH" not in forwarded


def test_run_once_invokes_cycle_with_built_dispatcher(monkeypatch) -> None:
    from oddish.dispatch import runner

    seen = {}

    async def _fake_cycle(dispatcher, *, max_workers, concurrency_for, **kwargs):
        seen["dispatcher"] = dispatcher
        seen["max_workers"] = max_workers
        return "cycle-result"

    monkeypatch.setattr(runner, "run_dispatch_cycle", _fake_cycle)
    result = asyncio.run(
        runner.run_once({"ODDISH_DISPATCH_MAX_WORKERS": "7"})
    )
    assert result == "cycle-result"
    assert isinstance(seen["dispatcher"], InProcessDispatcher)
    assert seen["max_workers"] == 7

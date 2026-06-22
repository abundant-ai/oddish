"""Off-Modal dispatcher entrypoint.

Builds a ``Dispatcher`` from configuration and drives the core dispatch cycle —
the command a Docker ``dispatcher`` service or a Kubernetes ``CronJob`` runs to
turn queue depth into workers off Modal. ``ODDISH_DISPATCH_ONCE`` runs a single
cycle (CronJob); otherwise it loops on the event trigger + fallback poll.

    ODDISH_DISPATCHER          inprocess (default) | docker | k8s
    ODDISH_WORKER_IMAGE        worker image (docker / k8s)
    ODDISH_DISPATCH_MAX_WORKERS  per-cycle spawn cap (default 256)
    ODDISH_DISPATCH_FALLBACK_SECONDS  fallback poll interval (default 20)
    ODDISH_K8S_NAMESPACE / ODDISH_K8S_SERVICE_ACCOUNT  (k8s)
    ODDISH_DOCKER_NETWORK      (docker)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Mapping

from oddish.config import settings
from oddish.dispatch.backends.docker import DockerPoolDispatcher
from oddish.dispatch.backends.inprocess import InProcessDispatcher
from oddish.dispatch.backends.k8s import K8sJobDispatcher
from oddish.dispatch.cycle import run_dispatch_cycle, run_dispatch_loop
from oddish.dispatch.ports import Dispatcher
from oddish.workers.queue.worker_job_dispatcher import stamp_dispatch_stage

logger = logging.getLogger(__name__)

# Dispatcher-only knobs that must not be forwarded into worker containers.
_DISPATCHER_ONLY_ENV = {
    "ODDISH_DISPATCHER",
    "ODDISH_WORKER_IMAGE",
    "ODDISH_DISPATCH_MAX_WORKERS",
    "ODDISH_DISPATCH_ONCE",
    "ODDISH_DISPATCH_FALLBACK_SECONDS",
    "ODDISH_K8S_NAMESPACE",
    "ODDISH_K8S_SERVICE_ACCOUNT",
    "ODDISH_DOCKER_NETWORK",
}


def worker_env_from(env: Mapping[str, str]) -> dict[str, str]:
    """The ``ODDISH_*`` config a spawned worker container inherits.

    Forwards the worker's own settings (DB url, harbor config, ...) but not the
    dispatcher-only knobs that selected/parameterized this dispatcher.
    """
    return {
        key: value
        for key, value in env.items()
        if key.startswith("ODDISH_") and key not in _DISPATCHER_ONLY_ENV
    }


def build_dispatcher_from_env(env: Mapping[str, str] = os.environ) -> Dispatcher:
    """Construct the configured off-Modal dispatcher (Modal is backend-wired)."""
    kind = env.get("ODDISH_DISPATCHER", "inprocess").lower()
    if kind == "inprocess":
        return InProcessDispatcher()
    if kind == "docker":
        image = env.get("ODDISH_WORKER_IMAGE")
        if not image:
            raise ValueError("ODDISH_WORKER_IMAGE is required for the docker dispatcher")
        return DockerPoolDispatcher(
            image=image,
            env=worker_env_from(env),
            network=env.get("ODDISH_DOCKER_NETWORK"),
        )
    if kind == "k8s":
        image = env.get("ODDISH_WORKER_IMAGE")
        if not image:
            raise ValueError("ODDISH_WORKER_IMAGE is required for the k8s dispatcher")
        return K8sJobDispatcher(
            image=image,
            namespace=env.get("ODDISH_K8S_NAMESPACE", "default"),
            env=worker_env_from(env),
            service_account=env.get("ODDISH_K8S_SERVICE_ACCOUNT"),
        )
    raise ValueError(f"unknown dispatcher: {kind!r}")


def _max_workers(env: Mapping[str, str]) -> int:
    return int(env.get("ODDISH_DISPATCH_MAX_WORKERS", "256"))


async def run_once(env: Mapping[str, str] = os.environ):
    """Run a single dispatch cycle (the CronJob / one-shot trigger)."""
    dispatcher = build_dispatcher_from_env(env)
    return await run_dispatch_cycle(
        dispatcher,
        max_workers=_max_workers(env),
        concurrency_for=settings.get_model_concurrency,
        on_stage=stamp_dispatch_stage,
    )


async def run_forever(env: Mapping[str, str] = os.environ) -> None:
    """Loop the dispatch cycle, woken by the event trigger or fallback poll."""
    dispatcher = build_dispatcher_from_env(env)
    await run_dispatch_loop(
        dispatcher,
        max_workers=_max_workers(env),
        concurrency_for=settings.get_model_concurrency,
        on_stage=stamp_dispatch_stage,
        fallback_interval=float(env.get("ODDISH_DISPATCH_FALLBACK_SECONDS", "20")),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if os.environ.get("ODDISH_DISPATCH_ONCE"):
        result = asyncio.run(run_once())
        logger.info(
            "dispatch cycle: spawned=%d waiting=%d",
            len(result.handles),
            len(result.why_waiting),
        )
    else:
        asyncio.run(run_forever())


if __name__ == "__main__":
    main()

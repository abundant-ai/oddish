"""Kubernetes dispatcher: one Job per worker (design spec §9-E).

Production-scale elastic fan-out off Modal. Each spawned worker is a Kubernetes
``Job`` running the oddish worker image, draining one ``queue_key`` then exiting
(``restartPolicy: Never``). The core dispatch cycle decides *how many* Jobs to
create per tick (queue-depth → Jobs, bounded by ``MAX_WORKERS_PER_POLL``); a
CronJob runs that cycle as the fallback trigger (see ``deploy/k8s/``).

Job names are **synchronous, durable handles**, so ``recover`` reattaches after
a control-plane restart (Nomad RecoverTask) — the property in-process/Docker
scalers lack (spec §14.3). Jobs are labelled so the reconciler can list-by-tag
and GC leaks (spec §6.5).

``import kubernetes`` is lazy (confined to client construction) so importing
this module never requires the Kubernetes SDK; the Job is built as a plain dict
(the client accepts a dict body), so manifest construction needs no SDK either.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator, Mapping, Sequence
from uuid import uuid4

from oddish.dispatch.ports import Dispatcher, WorkerHandle

logger = logging.getLogger(__name__)

_DNS1123_STRIP = re.compile(r"[^a-z0-9-]+")
_NAME_PREFIX = "oddish-worker"
MANAGED_LABEL_KEY = "oddish_managed"
QUEUE_KEY_LABEL = "oddish_queue_key"


def _sanitize_queue_key(queue_key: str) -> str:
    """Reduce a queue_key to a DNS-1123-safe label fragment."""
    cleaned = _DNS1123_STRIP.sub("-", queue_key.lower()).strip("-")
    return cleaned[:30] or "q"


class K8sJobDispatcher:
    name = "k8s"

    def __init__(
        self,
        *,
        image: str,
        namespace: str = "default",
        batch_api: Any | None = None,
        env: Mapping[str, str] | None = None,
        command: Sequence[str] = ("python", "-m", "oddish.workers.queue.worker"),
        backoff_limit: int = 0,
        ttl_seconds_after_finished: int = 300,
        service_account: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._image = image
        self._namespace = namespace
        self._batch_api = batch_api
        self._env = dict(env or {})
        self._command = list(command)
        self._backoff_limit = backoff_limit
        self._ttl = ttl_seconds_after_finished
        self._service_account = service_account
        self._labels = dict(labels or {})

    def _api(self):
        if self._batch_api is None:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            self._batch_api = client.BatchV1Api()
        return self._batch_api

    def _job_name(self, queue_key: str) -> str:
        suffix = uuid4().hex[:8]
        base = f"{_NAME_PREFIX}-{_sanitize_queue_key(queue_key)}-{suffix}"
        return base[:63].strip("-")

    def _job_manifest(self, queue_key: str, job_name: str) -> dict:
        labels = {
            "app": _NAME_PREFIX,
            MANAGED_LABEL_KEY: "true",
            QUEUE_KEY_LABEL: _sanitize_queue_key(queue_key),
            **self._labels,
        }
        env = [{"name": k, "value": v} for k, v in self._env.items()]
        env.append({"name": "ODDISH_WORKER_QUEUE_KEY", "value": queue_key})
        pod_spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "worker",
                    "image": self._image,
                    "command": self._command,
                    "env": env,
                }
            ],
        }
        if self._service_account:
            pod_spec["serviceAccountName"] = self._service_account
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "labels": labels},
            "spec": {
                "backoffLimit": self._backoff_limit,
                "ttlSecondsAfterFinished": self._ttl,
                "template": {"metadata": {"labels": labels}, "spec": pod_spec},
            },
        }

    async def spawn(self, *, spawn_plan: Sequence[str]) -> Sequence[WorkerHandle]:
        if not spawn_plan:
            return []
        api = self._api()
        handles: list[WorkerHandle] = []
        for queue_key in spawn_plan:
            job_name = self._job_name(queue_key)
            manifest = self._job_manifest(queue_key, job_name)
            await asyncio.to_thread(
                api.create_namespaced_job,
                namespace=self._namespace,
                body=manifest,
            )
            handles.append(
                WorkerHandle(
                    provider=self.name,
                    queue_key=queue_key,
                    id=job_name,
                    provisional=False,
                )
            )
        return handles

    async def check_active(
        self, handles: Sequence[WorkerHandle]
    ) -> AsyncIterator[WorkerHandle]:
        api = self._api()
        for handle in handles:
            if not handle.id:
                continue
            try:
                job = await asyncio.to_thread(
                    api.read_namespaced_job_status,
                    name=handle.id,
                    namespace=self._namespace,
                )
            except Exception:
                continue  # gone -> not active
            active = getattr(job.status, "active", None) or 0
            if active > 0:
                yield handle

    async def cancel(self, handles: Sequence[WorkerHandle]) -> int:
        api = self._api()
        cancelled = 0
        for handle in handles:
            if not handle.id:
                continue
            try:
                await asyncio.to_thread(
                    api.delete_namespaced_job,
                    name=handle.id,
                    namespace=self._namespace,
                    propagation_policy="Background",
                )
                cancelled += 1
            except Exception:
                continue  # already gone
        return cancelled

    async def recover(self, serialized: Mapping[str, Any]) -> WorkerHandle | None:
        handle = WorkerHandle.deserialize(serialized)
        if not handle.id:
            return None
        api = self._api()
        try:
            await asyncio.to_thread(
                api.read_namespaced_job_status,
                name=handle.id,
                namespace=self._namespace,
            )
        except Exception:
            return None
        return handle


_: Dispatcher = K8sJobDispatcher(image="placeholder")  # structural check

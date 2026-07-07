"""GKE execution backend for TPU trials. The ``harbor.environments.gke_auth``
import is lazy (confined to teardown) so importing this module never requires
the GKE client stack, and it tolerates harbor pins that predate that helper."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Iterator

from oddish.config import settings
from oddish.runtime.ports import Capabilities, ExecutionBackend, TpuSupport

logger = logging.getLogger(__name__)


class GkeBackend:
    name = "gke"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,
            tpu=TpuSupport(types=("v5e", "v6e"), max_chips_per_host=8),
            private_registry_pull=False,
            network_egress="allow",
            persistent_volumes=False,
            streaming_logs=False,
            memory_snapshot_fork=False,
            cold_start="minutes",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        # Provider defaults first, caller kwargs last (caller wins), matching
        # the Daytona spread. Names mirror ``GKEEnvironment.__init__``.
        return {
            "cluster_name": settings.gke_cluster_name,
            "region": settings.gke_region,
            "project_id": settings.gke_project_id,
            "namespace": settings.gke_namespace,
            "registry_location": settings.gke_registry_location,
            "registry_name": settings.gke_registry_name,
            "flex_start": settings.gke_flex_start,
            "pod_ready_timeout_sec": settings.gke_pod_ready_timeout_sec,
            **base_kwargs,
        }

    async def teardown(self, external_id: str) -> bool:
        try:
            namespace, pod = external_id.split("/", 1)
            # Imported lazily: the ADC helper only exists on GKE-enabled harbor
            # pins, and importing it at module load would break every other
            # backend on pins that lack it.
            from harbor.environments.gke_auth import build_core_api

            api = await asyncio.to_thread(
                build_core_api,
                settings.gke_cluster_name,
                settings.gke_region,
                settings.gke_project_id,
            )
            await asyncio.to_thread(
                api.delete_namespaced_pod,
                name=pod,
                namespace=namespace,
                grace_period_seconds=0,
            )
        except Exception:
            logger.warning(
                "GkeBackend.teardown: failed to terminate %s", external_id,
                exc_info=True,
            )
            return False
        logger.info("GkeBackend.teardown: terminated %s", external_id)
        return True

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        # Pod events are fetched on failure by the runner; no SDK-output
        # capture to tee here.
        yield None


_: ExecutionBackend = GkeBackend()  # structural conformance check at import

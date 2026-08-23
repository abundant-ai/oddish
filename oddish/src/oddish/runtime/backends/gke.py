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
from oddish.core.harbor_source import GKE_VARIANT_ID
from oddish.runtime.ports import Capabilities, ExecutionBackend, TpuSupport

logger = logging.getLogger(__name__)


class GkeBackend:
    name = "gke"

    # GKE trials run the GKE-enabled harbor-gke fork, which the lean default
    # Harbor omits. This is the backend that owns that binding: the blessed
    # ``gke`` variant image bakes harbor-gke, and submission routing stamps a
    # GKE trial's Harbor source so it dispatches onto that image (see
    # stamp_gke_harbor_source / HARBOR_VARIANTS).
    harbor_variant_id = GKE_VARIANT_ID

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
        # Precedence, stated exactly:
        # a submission kwarg (--environment-kwarg) beats the deployment
        # setting, because base_kwargs is spread last. A task.toml
        # [environment.kwargs] key CANNOT beat the deployment setting on this
        # path: harbor merges task kwargs as the base UNDER job kwargs, and
        # this dict is the job kwargs, so the deployment value always covers
        # the task's. Fixing that needs a defaults channel harbor does not
        # have; until then a task-level provisioning_mode is decorative here
        # and the per-submission kwarg is the override that works.
        # Provider defaults first, caller kwargs last (caller wins), matching
        # the Daytona spread. Names mirror ``GKEEnvironment.__init__``.
        #
        # ``provisioning_mode`` is the one key that selects accelerator
        # capacity -- 'flex-start', 'spot' or 'on-demand'. It needs no
        # reconciliation against the deployment default, because the spread
        # already does that whole job: a caller who names a mode overwrites
        # the default, and one key cannot contradict itself.
        #
        # The two booleans it replaced did need it. Naming one mode left the
        # other at its deployment default, and flex_start=true beside
        # spot=true asks for a node pool that cannot exist, so an explicit
        # request had to clear its opposite -- keyed on presence, because
        # spot=false was a statement about spot rather than a vote against a
        # flex-start default. Harbor removed the booleans, so that state is
        # unrepresentable and the reconciliation has nothing left to
        # reconcile.
        return {
            "cluster_name": settings.gke_cluster_name,
            "region": settings.gke_region,
            "project_id": settings.gke_project_id,
            "namespace": settings.gke_namespace,
            "registry_location": settings.gke_registry_location,
            "registry_name": settings.gke_registry_name,
            "provisioning_mode": settings.gke_provisioning_mode,
            "auto_build_missing_image": settings.gke_auto_build_missing_image,
            "auto_provision_cluster": settings.gke_auto_provision_cluster,
            "pod_ready_timeout_sec": settings.gke_pod_ready_timeout_sec,
            # Refuse the trial-time gcloud Cloud Build fallback on a task-image
            # miss: it shells out to a subprocess and surfaces a raw
            # FileNotFoundError. With this set Harbor raises an actionable
            # "image not found" instead. The hosted model builds/pushes task
            # images ahead of the run, so a miss is an error, not a build cue.
            "require_prebuilt_image": True,
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
                "GkeBackend.teardown: failed to terminate %s",
                external_id,
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

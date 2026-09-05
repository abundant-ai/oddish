"""Numinous Cloud execution backend.

Numinous is a sandbox cloud built for exactly this workload: microVM
sandboxes with a real Docker daemon inside, hard server-side TTLs, typed
failure causes on every terminal state, launch-token idempotent creation,
artifact export that survives teardown, and per-second metering queryable by
trial label. Everything is non-preemptible.

What this deletes from oddish's operational surface, mapped to incidents:
- no stale-sandbox reaper: expires_at is enforced provider-side and teardown
  returns proof (vs. the Daytona reaper, PR #1077, and the auto-stop tuning
  saga, PRs #923/#1188 — including the mid-upload reaping of exp e127df61)
- no error-string classification: terminal causes arrive typed
  (user_image_build_failed vs provider_capacity vs provider_infra), and
  provider_* seconds are unbilled by contract
- no cost-span estimation: GET /usage?label=trial_id:<id> is the invoice

Configuration:
    NUMINOUS_API_URL   control-plane base URL
    NUMINOUS_API_KEY   bearer key
    ODDISH_NUMINOUS_ENABLED=1 registers the backend (cheap-first, before
    Daytona) — see runtime/registry.py.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any, Iterator

from oddish.config import settings
from oddish.runtime.ports import Capabilities, ExecutionBackend, GpuSupport

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:8400"


def _api() -> tuple[str, dict[str, str]]:
    url = os.environ.get("NUMINOUS_API_URL", _DEFAULT_URL).rstrip("/")
    key = os.environ.get("NUMINOUS_API_KEY", "nk_local_dev")
    return url, {"Authorization": f"Bearer {key}"}


class NuminousBackend:
    name = "numinous"

    def capabilities(self) -> Capabilities:
        # GPU lane is opt-in behind ODDISH_NUMINOUS_GPU_ENABLED. When on, the
        # backend advertises the SKUs the control plane knows how to rent
        # (RunPod secure cloud + our shared gpu_mux plane). Turning the flag
        # on routes GPU trials to Numinous ahead of Modal in the cheap-first
        # order the registry defines.
        gpu: GpuSupport | None = None
        if getattr(settings, "numinous_gpu_enabled", False):
            gpu = GpuSupport(
                # Order matters: fallback-ordered list per Capabilities spec.
                # H100 is our headline for SWE-marathon; L40S is the cheap
                # inference lane; RTX_4090 is the shared-mux fake for CI
                # smoke tests where CUDA is not actually required.
                accelerators=("H100", "H200", "A100", "L40S", "A10", "RTX_4090"),
                max_count=8,
            )
        return Capabilities(
            gpu=gpu,
            private_registry_pull=True,
            network_egress="configurable",
            persistent_volumes=True,  # named volumes, idempotent create, $0.10/GiB-mo
            streaming_logs=True,
            memory_snapshot_fork=True,  # firecracker plane; docker driver approximates
            cold_start="seconds",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        # TTLs, labels and idempotency are handled by the Harbor environment
        # (harbor.environments.numinous); nothing to override here beyond
        # making trials attributable.
        return {**base_kwargs}

    async def set_labels(self, external_id: str, labels: dict[str, str | None]) -> bool:
        """Merge labels onto a sandbox (any state). Used to stamp the graded
        outcome (oddish.reward / oddish.status) after the trial ends, when the
        environment object is gone or lived in another process. Metadata:
        never raises."""
        if not external_id or not labels:
            return False
        try:
            import httpx

            url, headers = _api()
            async with httpx.AsyncClient(
                base_url=url, headers=headers, timeout=30
            ) as client:
                r = await client.patch(
                    f"/v1/sandboxes/{external_id}/labels", json={"labels": labels}
                )
                if r.status_code >= 400:
                    logger.info(
                        "metric=numinous.label_stamp_refused external_id=%s status=%s",
                        external_id,
                        r.status_code,
                    )
                    return False
                return True
        except Exception:
            logger.info(
                "metric=numinous.label_stamp_failed external_id=%s", external_id
            )
            return False

    async def stamp_trial_outcome(
        self,
        trial_id: str,
        *,
        reward: float | None,
        status: str,
        error: str | None = None,
    ) -> int:
        """Stamp the graded outcome on every sandbox this trial created.

        Addresses sandboxes by the ``oddish.trial_id`` label the environment
        wrote at create, so it needs no hook plumbing and works whether the
        trial ran in-process or in the ephemeral child. Returns how many
        sandboxes were stamped. Metadata: never raises."""
        if not trial_id:
            return 0
        labels: dict[str, str | None] = {
            "oddish.status": status,
            "oddish.reward": None if reward is None else str(reward),
        }
        if error:
            labels["oddish.error"] = str(error)[:256]
        try:
            import httpx

            url, headers = _api()
            async with httpx.AsyncClient(
                base_url=url, headers=headers, timeout=30
            ) as client:
                r = await client.get(
                    "/v1/sandboxes",
                    params={"label": f"oddish.trial_id:{trial_id}", "limit": 50},
                )
                if r.status_code >= 400:
                    logger.info(
                        "metric=numinous.outcome_stamp trial_id=%s lookup_status=%s",
                        trial_id,
                        r.status_code,
                    )
                    return 0
                body = r.json()
                items = body.get("items", body) if isinstance(body, dict) else body
                stamped = 0
                for sb in items or []:
                    pr = await client.patch(
                        f"/v1/sandboxes/{sb['id']}/labels", json={"labels": labels}
                    )
                    stamped += pr.status_code < 400
                logger.info(
                    "metric=numinous.outcome_stamp trial_id=%s sandboxes=%d stamped=%d",
                    trial_id,
                    len(items or []),
                    stamped,
                )
                return stamped
        except Exception:
            logger.info("metric=numinous.outcome_stamp trial_id=%s failed", trial_id)
            return 0

    async def settle_trial(
        self,
        trial_id: str,
        *,
        reward: float | None,
        status: str,
    ) -> bool:
        """Record the trial's final verdict once oddish has settled it (after
        any retries). Attempt stamps are provisional; this is the word the
        provider's console shows for the trial as a whole. Metadata: never
        raises. Returns whether the provider accepted it."""
        if not trial_id or (reward is None and not status):
            return False
        body: dict[str, object] = {
            "value": reward,
            "label": None if reward is not None else status,
            "kind": "reward",
            "status": status,
            "force": True,
        }
        try:
            import httpx

            url, headers = _api()
            async with httpx.AsyncClient(
                base_url=url, headers=headers, timeout=30
            ) as client:
                r = await client.put(f"/v1/trials/{trial_id}/outcome", json=body)
                logger.info(
                    "metric=numinous.trial_settle trial_id=%s status=%s http=%s",
                    trial_id,
                    status,
                    r.status_code,
                )
                return r.status_code < 400
        except Exception:
            logger.info("metric=numinous.trial_settle trial_id=%s failed", trial_id)
            return False

    async def teardown(self, external_id: str) -> bool:
        """Best-effort terminate by sandbox id. The response carries a
        teardown proof; expired sandboxes are already gone (the TTL is
        enforced provider-side), which we count as success."""
        if not external_id:
            return False
        try:
            import httpx

            url, headers = _api()
            async with httpx.AsyncClient(base_url=url, headers=headers) as client:
                r = await client.delete(f"/v1/sandboxes/{external_id}")
                if r.status_code == 404:
                    logger.info(
                        "metric=numinous.sandbox_gone phase=teardown external_id=%s",
                        external_id,
                    )
                    return True
                r.raise_for_status()
                # The terminate was accepted (2xx): teardown succeeded, matching
                # the shared backend contract (orphan cleanup / strict harvest
                # only need "is it gone"). verified_absent is extra proof we log
                # but do not gate on — a running-state teardown can legitimately
                # return proof=false while still having terminated the sandbox.
                proof = r.json().get("teardown_proof") or {}
                logger.info(
                    "NuminousBackend.teardown: terminated %s verified_absent=%s",
                    external_id,
                    bool(proof.get("verified_absent")),
                )
                return True
        except Exception:
            logger.exception(
                "NuminousBackend.teardown: failed to terminate %s", external_id
            )
            return False

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        # Build logs are first-class API objects (templates carry build_log;
        # sandboxes carry typed causes), so there is no SDK output to scrape.
        yield None


_: ExecutionBackend = NuminousBackend()  # structural conformance check

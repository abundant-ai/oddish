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

from oddish.runtime.ports import Capabilities, ExecutionBackend

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:8400"


def _api() -> tuple[str, dict[str, str]]:
    url = os.environ.get("NUMINOUS_API_URL", _DEFAULT_URL).rstrip("/")
    key = os.environ.get("NUMINOUS_API_KEY", "nk_local_dev")
    return url, {"Authorization": f"Bearer {key}"}


class NuminousBackend:
    name = "numinous"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,  # GPU lane ships behind a separate flag
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
                proof = (r.json().get("teardown_proof") or {})
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

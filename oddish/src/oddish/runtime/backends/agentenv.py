"""AgentENV execution backend.

AgentENV speaks the E2B-compatible API, so Harbor runs it through
``EnvironmentType.E2B``. This backend is Oddish's capability/configuration shim:
it keeps hosted ``--env e2b`` opt-in, advertises snapshot/fork support, and
performs best-effort teardown without importing an SDK at module load.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any, Iterator

from oddish.config import settings
from oddish.runtime.ports import Capabilities, ExecutionBackend

logger = logging.getLogger(__name__)


class AgentenvBackend:
    # Keep this equal to Harbor's EnvironmentType.E2B. "agentenv" is the backing
    # service; "e2b" is the compatibility environment Harbor already knows.
    name = "e2b"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,
            private_registry_pull=False,
            network_egress="allow",
            persistent_volumes=False,
            streaming_logs=True,
            memory_snapshot_fork=True,
            cold_start="seconds",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        # Harbor's E2BEnvironment reads E2B_* process env vars through the E2B
        # SDK rather than environment.kwargs. Keep caller kwargs intact, and
        # make local/off-Modal workers work when only ODDISH_AGENTENV_* is set.
        api_url = settings.agentenv_api_url
        sandbox_url = settings.agentenv_sandbox_url or api_url
        if api_url:
            os.environ.setdefault("E2B_API_URL", api_url)
            os.environ.setdefault("E2B_SANDBOX_URL", sandbox_url or api_url)
            os.environ.setdefault("E2B_API_KEY", settings.agentenv_api_key)
            os.environ.setdefault("E2B_ACCESS_TOKEN", settings.agentenv_api_key)
        return dict(base_kwargs)

    async def teardown(self, external_id: str) -> bool:
        if not external_id:
            return False
        api_url = settings.agentenv_api_url or os.environ.get("E2B_API_URL")
        api_key = os.environ.get("E2B_API_KEY") or settings.agentenv_api_key
        if not api_url:
            return False
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.delete(
                    f"{api_url.rstrip('/')}/sandboxes/{external_id}",
                    headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                )
                response.raise_for_status()
        except Exception:
            logger.exception(
                "AgentenvBackend.teardown: failed to terminate %s", external_id
            )
            return False
        logger.info("AgentenvBackend.teardown: terminated %s", external_id)
        return True

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        # The E2B/AgentENV SDK path logs through Harbor's trial output; no
        # provider SDK stream needs a Modal-style tee here.
        yield None


_: ExecutionBackend = AgentenvBackend()  # structural conformance check at import

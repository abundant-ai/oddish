from __future__ import annotations

from dataclasses import dataclass

import logging

from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient

log = logging.getLogger("oddish.cc_chat.provisioner")


@dataclass
class Provisioner:
    client: DaytonaClient

    async def create(
        self,
        *,
        env_vars: dict[str, str],
        auto_stop_minutes: int,
        daytona_session_id: str,
    ) -> CreatedSandbox:
        sbx = await self.client.create_sandbox(
            env_vars=env_vars, auto_stop_minutes=auto_stop_minutes
        )
        await self.client.create_session(sbx, session_id=daytona_session_id)
        return sbx


async def delete_sandbox_quietly(
    client: DaytonaClient, sandbox: CreatedSandbox
) -> None:
    try:
        await client.delete_sandbox(sandbox)
    except Exception:
        log.exception("delete_sandbox raised sandbox_id=%s", sandbox.id)

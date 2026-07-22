"""Provision a sandbox that can run `oddish pull`: install the oddish CLI and
inject a short-lived READ key. Returns a SandboxAnalyzerLLMClient to pass into
AnalyzerBlock(client=...)."""

from __future__ import annotations

import logging
import os

from api.services.blocks.analyzer.sandbox_llm_client import SandboxAnalyzerLLMClient
from oddish.blocks.analyzer.analyzer_llm_client import resolve_analyzer_api_key
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.cc_chat.daytona_client import RealDaytonaClient
from api.services.cc_chat.provisioner import Provisioner, delete_sandbox_quietly
from oddish.core.api_keys import mint_internal_read_key
from oddish.db import get_session
from oddish.db.models import APIKeyModel, generate_id

logger = logging.getLogger(__name__)

_TTL_MINUTES = 45
_DAYTONA_SESSION_ID = "pre-trial"
_AUTO_STOP_MINUTES = 15
_AUTO_DELETE_MINUTES = 30


async def _revoke_key_quietly(api_key_id: str) -> None:
    """Best-effort delete of the minted READ key; never raises (the key's
    TTL is the backstop if this write fails)."""
    try:
        async with get_session() as session:
            key = await session.get(APIKeyModel, api_key_id)
            if key is not None:
                await session.delete(key)
                await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to revoke pre-trial key %s (will auto-expire): %s",
            api_key_id,
            exc,
        )


async def provision_oddish_sandbox_client(
    *, org_id: str, model: str, api_key: str | None, api_base_url: str
) -> SandboxAnalyzerLLMClient:
    async with get_session() as session:
        api_key_id, raw_key = await mint_internal_read_key(
            session,
            org_id=org_id,
            name=f"pre-trial:{generate_id()}",
            ttl_minutes=_TTL_MINUTES,
        )

    daytona_client = RealDaytonaClient(api_key=os.environ["DAYTONA_API_KEY"])
    env_vars = {
        "ANTHROPIC_API_KEY": resolve_analyzer_api_key(api_key) or "",
        "ODDISH_API_KEY": raw_key,
        "ODDISH_API_BASE_URL": api_base_url,
    }
    if model:
        env_vars["ANTHROPIC_MODEL"] = model
    # Any failure after the mint must clean up what already exists (revoke the
    # key, delete a half-provisioned sandbox): the caller never gets a client,
    # so its aclose() can never run.
    sandbox = None
    try:
        sandbox = await Provisioner(client=daytona_client).create(
            env_vars=env_vars,
            auto_stop_minutes=_AUTO_STOP_MINUTES,
            auto_delete_minutes=_AUTO_DELETE_MINUTES,
            labels={"app": "pre-trial", "session_id": generate_id()},
            daytona_session_id=_DAYTONA_SESSION_ID,
        )
        runtime = ClaudeCodeRuntime()
        await runtime.install(daytona_client, sandbox)
        await runtime.install_oddish_cli(
            daytona_client, sandbox, api_key=raw_key, api_base_url=api_base_url
        )
    except BaseException:
        if sandbox is not None:
            await delete_sandbox_quietly(daytona_client, sandbox)
        await _revoke_key_quietly(api_key_id)
        raise
    return SandboxAnalyzerLLMClient(
        sandbox=sandbox,
        daytona_client=daytona_client,
        runtime=runtime,
        daytona_session_id=_DAYTONA_SESSION_ID,
    )

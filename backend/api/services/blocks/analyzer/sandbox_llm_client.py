"""The Daytona-sandbox AnalyzerLLMClient backend.

Lives in backend/ rather than beside the API/OpenAI clients in
``oddish.blocks.analyzer.analyzer_llm_client`` because it needs cc_chat's
provisioner and Daytona client, which are hosted-layer only. Core reaches it
through the factory hook it registers on import, so a backend-free worker
still runs every non-sandbox block.
"""

from __future__ import annotations

import json
import os
from typing import AsyncIterator

from oddish.blocks.analyzer.analyzer_llm_client import (
    AnalyzerLLMClient,
    register_sandbox_client_factory,
    resolve_analyzer_api_key,
)
from oddish.db import generate_id

from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.cc_chat.daytona_client import (
    CreatedSandbox,
    DaytonaClient,
    RealDaytonaClient,
)
from api.services.cc_chat.provisioner import Provisioner, delete_sandbox_quietly

_DAYTONA_SESSION_ID = "analyzer"
_AUTO_STOP_MINUTES = 15
_AUTO_DELETE_MINUTES = 30


class SandboxAnalyzerLLMClient:
    """Daytona-sandbox backend: runs claude-code and yields one JSON string per
    stream-json event. Provisioning happens in ``create_sandbox_llm_client`` (an
    async factory) -- constructors cannot be awaited."""

    def __init__(
        self,
        *,
        sandbox: CreatedSandbox,
        daytona_client: DaytonaClient,
        runtime: ClaudeCodeRuntime,
        daytona_session_id: str = _DAYTONA_SESSION_ID,
    ) -> None:
        self._sandbox = sandbox
        self._client = daytona_client
        self._runtime = runtime
        self._session_id = daytona_session_id

    async def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]:
        async for event in self._runtime.stream_chat(
            self._client,
            self._sandbox,
            content=prompt,
            claude_session_id=None,
            daytona_session_id=self._session_id,
            system_prompt=system_prompt,
        ):
            yield json.dumps(event)

    async def _download_file(self, path: str) -> bytes:
        return await self._client.download_file(self._sandbox, src_path=path)

    async def aclose(self) -> None:
        await delete_sandbox_quietly(self._client, self._sandbox)


async def create_sandbox_llm_client(
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> AnalyzerLLMClient:
    daytona_client = RealDaytonaClient(api_key=os.environ["DAYTONA_API_KEY"])
    env_vars = {"ANTHROPIC_API_KEY": resolve_analyzer_api_key(api_key) or ""}
    if model:
        env_vars["ANTHROPIC_MODEL"] = model
    sandbox = await Provisioner(client=daytona_client).create(
        env_vars=env_vars,
        auto_stop_minutes=_AUTO_STOP_MINUTES,
        auto_delete_minutes=_AUTO_DELETE_MINUTES,
        labels={"app": "analyzer", "session_id": generate_id()},
        daytona_session_id=_DAYTONA_SESSION_ID,
    )
    runtime = ClaudeCodeRuntime()
    await runtime.install(daytona_client, sandbox)
    return SandboxAnalyzerLLMClient(
        sandbox=sandbox, daytona_client=daytona_client, runtime=runtime
    )


register_sandbox_client_factory(create_sandbox_llm_client)

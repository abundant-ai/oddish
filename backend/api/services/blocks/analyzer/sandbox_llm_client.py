"""The Daytona-sandbox AnalyzerLLMClient backend.

Lives in backend/ rather than beside the API/OpenAI clients in
``oddish.blocks.analyzer.analyzer_llm_client`` because it needs cc_chat's
provisioner and Daytona client, which are hosted-layer only. Core reaches it
through the factory hook it registers on import, so a backend-free worker
still runs every non-sandbox block.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import shlex
from typing import AsyncIterator

from oddish.analyze.analysis_cost import AnalysisUsage, parse_cli_usage
from oddish.blocks.analyzer.analyzer_llm_client import (
    AnalyzerLLMClient,
    SandboxConfig,
    register_sandbox_client_factory,
    resolve_analyzer_api_key,
)
from oddish.core.api_keys import mint_internal_api_key
from oddish.db import generate_id, get_session
from oddish.db.models import APIKeyModel, APIKeyScope

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
_ODDISH_KEY_TTL_MINUTES = 45

logger = logging.getLogger(__name__)


async def _revoke_key_quietly(api_key_id: str) -> None:
    try:
        async with get_session() as session:
            key = await session.get(APIKeyModel, api_key_id)
            if key is not None:
                await session.delete(key)
                await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to revoke analyzer sandbox key %s (will auto-expire): %s",
            api_key_id,
            exc,
        )


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
        model: str | None = None,
        json_schema: str | None = None,
        add_dirs: tuple[str, ...] = (),
        daytona_session_id: str = _DAYTONA_SESSION_ID,
        internal_api_key_id: str | None = None,
    ) -> None:
        self._sandbox = sandbox
        # Public: lets callers (for example the analysis log) report which
        # Daytona sandbox a run executes in.
        self.sandbox_id = sandbox.id
        self._client = daytona_client
        self._runtime = runtime
        self._model = model
        self._json_schema = json_schema
        self._add_dirs = add_dirs
        self._session_id = daytona_session_id
        self._internal_api_key_id = internal_api_key_id
        # Filled from claude-code's stream-json ``result`` event, which carries
        # the run's native cost/usage.
        self.last_usage: AnalysisUsage | None = None

    async def stream(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> AsyncIterator[str]:
        stream_kwargs = {
            "content": prompt,
            "claude_session_id": None,
            "daytona_session_id": self._session_id,
            "system_prompt": system_prompt,
            "json_schema": self._json_schema,
        }
        if self._add_dirs:
            stream_kwargs["add_dirs"] = self._add_dirs
        async for event in self._runtime.stream_chat(
            self._client, self._sandbox, **stream_kwargs
        ):
            if event.get("type") == "result":
                self.last_usage = parse_cli_usage(
                    event,
                    self._model,
                )
            yield json.dumps(event)

    async def _download_file(self, path: str) -> bytes:
        return await self._client.download_file(self._sandbox, src_path=path)

    async def aclose(self) -> None:
        try:
            await delete_sandbox_quietly(self._client, self._sandbox)
        finally:
            if self._internal_api_key_id is not None:
                await _revoke_key_quietly(self._internal_api_key_id)
                self._internal_api_key_id = None


async def create_sandbox_llm_client(
    *,
    model: str | None = None,
    api_key: str | None = None,
    sandbox_config: SandboxConfig,
) -> AnalyzerLLMClient:
    internal_api_key_id = None
    internal_api_key = None
    if sandbox_config.install_oddish_cli:
        if not sandbox_config.oddish_org_id or not sandbox_config.oddish_api_base_url:
            raise ValueError(
                "install_oddish_cli requires oddish_org_id and oddish_api_base_url"
            )
        async with get_session() as session:
            try:
                internal_scope = APIKeyScope(sandbox_config.oddish_api_scope)
            except ValueError as exc:
                raise ValueError(
                    f"Unsupported analyzer Oddish API scope: "
                    f"{sandbox_config.oddish_api_scope!r}"
                ) from exc
            internal_api_key_id, internal_api_key = await mint_internal_api_key(
                session,
                org_id=sandbox_config.oddish_org_id,
                name=f"analyzer:{generate_id()}",
                ttl_minutes=_ODDISH_KEY_TTL_MINUTES,
                scope=internal_scope,
            )

    daytona_client = RealDaytonaClient(
        api_key=os.environ["DAYTONA_API_KEY"],
        snapshot=sandbox_config.snapshot,
    )
    env_vars = {"ANTHROPIC_API_KEY": resolve_analyzer_api_key(api_key) or ""}
    if sandbox_config.reasoning_effort:
        env_vars["CLAUDE_CODE_EFFORT_LEVEL"] = sandbox_config.reasoning_effort
    if sandbox_config.trajectory_tail_bytes is not None:
        env_vars["ODDISH_QUERY_TRAJ_TAIL_BYTES"] = str(
            sandbox_config.trajectory_tail_bytes
        )
    if sandbox_config.oddish_api_key:
        env_vars["ODDISH_API_KEY"] = sandbox_config.oddish_api_key
    if sandbox_config.oddish_api_base_url:
        env_vars["ODDISH_API_BASE_URL"] = sandbox_config.oddish_api_base_url
    if internal_api_key:
        env_vars.update(
            {
                "ODDISH_API_KEY": internal_api_key,
                "ODDISH_API_BASE_URL": sandbox_config.oddish_api_base_url or "",
            }
        )
    if model:
        env_vars["ANTHROPIC_MODEL"] = model
    sandbox = None
    try:
        sandbox = await Provisioner(client=daytona_client).create(
            env_vars=env_vars,
            auto_stop_minutes=(sandbox_config.auto_stop_minutes or _AUTO_STOP_MINUTES),
            auto_delete_minutes=(
                sandbox_config.auto_delete_minutes or _AUTO_DELETE_MINUTES
            ),
            labels={
                "app": sandbox_config.session_id,
                "session_id": generate_id(),
                **sandbox_config.labels,
            },
            daytona_session_id=sandbox_config.session_id,
        )
        runtime = ClaudeCodeRuntime()
        await runtime.install(daytona_client, sandbox)
        for path, content in sandbox_config.files_to_upload.items():
            parent = posixpath.dirname(path)
            if parent:
                exit_code, output = await daytona_client.exec_sync(
                    sandbox, command=f"mkdir -p -- {shlex.quote(parent)}"
                )
                if exit_code != 0:
                    raise RuntimeError(
                        "analyzer sandbox upload directory setup failed "
                        f"(exit={exit_code}, path={parent!r}): {output[-500:]}"
                    )
            await daytona_client.upload_file(sandbox, dest_path=path, content=content)
        for command in sandbox_config.setup_commands:
            exit_code, output = await daytona_client.exec_sync(sandbox, command=command)
            if exit_code != 0:
                raise RuntimeError(
                    f"analyzer sandbox setup failed (exit={exit_code}): {output[-500:]}"
                )
        if sandbox_config.install_oddish_cli:
            await runtime.install_oddish_cli(
                daytona_client,
                sandbox,
                api_key=internal_api_key or "",
                api_base_url=sandbox_config.oddish_api_base_url or "",
            )
        elif sandbox_config.oddish_api_key:
            await runtime.install_oddish_cli(
                daytona_client,
                sandbox,
                api_key=sandbox_config.oddish_api_key,
                api_base_url=sandbox_config.oddish_api_base_url or "",
            )
    except BaseException:
        if sandbox is not None:
            await delete_sandbox_quietly(daytona_client, sandbox)
        if internal_api_key_id is not None:
            await _revoke_key_quietly(internal_api_key_id)
        raise
    return SandboxAnalyzerLLMClient(
        sandbox=sandbox,
        daytona_client=daytona_client,
        runtime=runtime,
        model=model,
        json_schema=sandbox_config.json_schema,
        add_dirs=sandbox_config.add_dirs,
        daytona_session_id=sandbox_config.session_id,
        internal_api_key_id=internal_api_key_id,
    )


register_sandbox_client_factory(create_sandbox_llm_client)

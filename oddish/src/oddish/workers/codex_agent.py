from __future__ import annotations

from harbor.agents.installed.codex import Codex


class AzureCompatibleCodex(Codex):
    """Codex runner variant for Azure OpenAI-compatible endpoints.

    Harbor's Codex wrapper currently hard-codes ``--enable unified_exec``. With
    Azure OpenAI-compatible endpoints that sends Codex CLI down the websocket
    Responses route (``wss://.../openai/v1/responses``), which Azure returns as
    a 302 before the agent reads the task. Keep Harbor on main and only rewrite
    that transport flag for Azure-routed Oddish trials.
    """

    async def exec_as_agent(
        self,
        environment,
        command,
        env=None,
        cwd=None,
        timeout_sec=None,
    ):
        if "codex exec " in command and "--enable unified_exec " in command:
            command = command.replace("--enable unified_exec ", "--disable unified_exec ")
        return await super().exec_as_agent(
            environment,
            command,
            env=env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )

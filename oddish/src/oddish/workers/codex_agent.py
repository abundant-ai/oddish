from __future__ import annotations

import shlex

from harbor.agents.installed.codex import Codex


_AZURE_CODEX_PROVIDER = "oddish_azure_openai"


def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class AzureCompatibleCodex(Codex):
    """Codex runner variant for Azure OpenAI-compatible endpoints.

    Azure OpenAI-compatible endpoints currently reject Codex CLI's websocket
    Responses route (``wss://.../openai/v1/responses``) with a 302 before the
    agent reads the task. Configure Codex with an explicit OpenAI-compatible
    provider that disables websockets so it uses the HTTP Responses stream.
    """

    def _azure_provider_config_command(self) -> str:
        base_url = self._get_env("OPENAI_BASE_URL")
        if not base_url:
            return ""

        lines = [
            "",
            "# Oddish Azure OpenAI transport: Azure accepts HTTP Responses here,",
            "# but the Codex websocket Responses route returns 302.",
            f"model_provider = {_toml_quote(_AZURE_CODEX_PROVIDER)}",
            f"[model_providers.{_AZURE_CODEX_PROVIDER}]",
            'name = "Azure OpenAI (Oddish)"',
            f"base_url = {_toml_quote(base_url)}",
            'env_key = "OPENAI_API_KEY"',
            'wire_api = "responses"',
            "supports_websockets = false",
        ]
        payload = "\n".join(lines) + "\n"
        return f"cat >>\"$CODEX_HOME/config.toml\" <<'ODDISH_AZURE_CODEX_TOML'\n{payload}ODDISH_AZURE_CODEX_TOML\n"

    def _maybe_append_provider_config(self, command: str) -> str:
        if 'CODEX_HOME/config.toml' not in command:
            return command
        provider_config = self._azure_provider_config_command()
        if not provider_config or _AZURE_CODEX_PROVIDER in command:
            return command
        return command.rstrip() + "\n" + provider_config

    async def exec_as_agent(
        self,
        environment,
        command,
        env=None,
        cwd=None,
        timeout_sec=None,
    ):
        command = self._maybe_append_provider_config(command)
        if "codex exec " in command and "--enable unified_exec " in command:
            command = command.replace("--enable unified_exec ", "--disable unified_exec ")
        if "codex exec " in command and " -c model_provider=" not in command:
            command = command.replace(
                "codex exec ",
                f"codex exec -c model_provider={shlex.quote(_toml_quote(_AZURE_CODEX_PROVIDER))} ",
                1,
            )
        return await super().exec_as_agent(
            environment,
            command,
            env=env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )

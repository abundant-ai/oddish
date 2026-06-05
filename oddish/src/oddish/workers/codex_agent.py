from __future__ import annotations

import re
import shlex

from harbor.agents.installed.codex import Codex
from harbor.agents.installed.base import NonZeroAgentExitCodeError


_AZURE_CODEX_PROVIDER = "oddish_azure_openai"
_AZURE_CODEX_RETRY_CONFIG_PARAMS = {
    "text.verbosity": "model_verbosity",
}
_SUPPORTED_VALUES_RE = re.compile(
    r"Supported values are:\s*(?P<values>(?:'[^']+'\s*,?\s*)+)"
)


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

    def _ensure_codex_config_override(
        self, command: str, key: str, value: str
    ) -> str:
        if "codex exec " not in command or f" -c {key}=" in command:
            return command
        return command.replace(
            "codex exec ",
            f"codex exec -c {key}={shlex.quote(_toml_quote(value))} ",
            1,
        )

    def _set_codex_config_override(self, command: str, key: str, value: str) -> str:
        quoted_value = shlex.quote(_toml_quote(value))
        pattern = re.compile(
            rf"(?P<prefix>\s-c\s+{re.escape(key)}=)(?:'[^']*'|\"[^\"]*\"|\S+)"
        )
        if pattern.search(command):
            return pattern.sub(
                lambda match: f"{match.group('prefix')}{quoted_value}",
                command,
                count=1,
            )
        return self._ensure_codex_config_override(command, key, value)

    def _retry_command_for_unsupported_config(
        self, command: str, error_text: str
    ) -> str | None:
        normalized = error_text.replace('\\"', '"').replace("\\n", "\n")
        config_key = None
        for api_param, codex_config_key in _AZURE_CODEX_RETRY_CONFIG_PARAMS.items():
            if f'"param": "{api_param}"' in normalized:
                config_key = codex_config_key
                break
        if not config_key:
            return None

        supported_match = _SUPPORTED_VALUES_RE.search(normalized)
        if not supported_match:
            return None
        supported_values = re.findall(r"'([^']+)'", supported_match.group("values"))
        if not supported_values:
            return None

        return self._set_codex_config_override(command, config_key, supported_values[0])

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
        command = self._ensure_codex_config_override(
            command, "model_provider", _AZURE_CODEX_PROVIDER
        )
        try:
            return await super().exec_as_agent(
                environment,
                command,
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )
        except NonZeroAgentExitCodeError as exc:
            retry_command = self._retry_command_for_unsupported_config(
                command, str(exc)
            )
            if retry_command is None or retry_command == command:
                raise
            return await super().exec_as_agent(
                environment,
                retry_command,
                env=env,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )

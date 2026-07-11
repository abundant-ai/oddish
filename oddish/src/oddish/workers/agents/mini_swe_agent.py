from __future__ import annotations

import re
import shlex
import uuid

from harbor.agents.installed.mini_swe_agent import MiniSweAgent

from oddish.config import meta_bare_model_id


_META_CONFIG_PATH = "/tmp/oddish-meta-mini-swe-agent.yaml"
_META_API_DOMAIN = "api.ai.meta.com"


def _slugify_session_part(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "oddish-eval"


class OddishMetaMiniSweAgent(MiniSweAgent):
    """mini-swe-agent wrapper for Meta's OpenAI-compatible eval endpoint."""

    @classmethod
    def required_outbound_domains(
        cls,
        model_name: str | None = None,
        kwargs: dict | None = None,
    ) -> list[str]:
        return [_META_API_DOMAIN]

    def _meta_session_id(self) -> str:
        explicit = (
            self._get_env("ODDISH_META_SESSION_ID")
            or self._get_env("META_SESSION_ID")
            or ""
        ).strip()
        if explicit:
            return explicit

        eval_name = (
            self._get_env("ODDISH_META_EVAL_NAME")
            or self._get_env("META_EVAL_NAME")
            or "oddish-eval"
        )
        return f"{_slugify_session_part(eval_name)}--{uuid.uuid4().hex[:12]}"

    def _litellm_model_name(self) -> str | None:
        model_name = str(self.model_name or "").strip()
        if not model_name:
            return None
        return f"openai/{meta_bare_model_id(model_name)}"

    async def _write_meta_config(self, environment, env: dict | None) -> None:
        session_id = self._meta_session_id()
        quoted_path = shlex.quote(_META_CONFIG_PATH)
        heredoc_marker = f"ODDISH_META_MSWEA_CONFIG_{uuid.uuid4().hex[:8]}"
        config_yaml = (
            "model:\n"
            "  model_kwargs:\n"
            "    extra_headers:\n"
            f"      x-session-id: {session_id!r}\n"
        )
        command = (
            f"cat > {quoted_path} <<'{heredoc_marker}'\n{config_yaml}{heredoc_marker}\n"
        )
        await super().exec_as_agent(environment, command=command, env=env)

    def _patch_meta_command(self, command: str) -> str:
        model_name = self._litellm_model_name()
        if model_name:
            command = re.sub(
                r"--model=\S+",
                f"--model={shlex.quote(model_name)}",
                command,
                count=1,
            )

        config_flags = f"-c {shlex.quote(_META_CONFIG_PATH)} "
        if " -c " not in command:
            config_flags = "-c mini " + config_flags
        return command.replace(
            "--exit-immediately", config_flags + "--exit-immediately", 1
        )

    async def exec_as_agent(
        self,
        environment,
        command,
        env=None,
        cwd=None,
        timeout_sec=None,
    ):
        if "mini-swe-agent --yolo " in command:
            await self._write_meta_config(environment, env)
            command = self._patch_meta_command(command)

        return await super().exec_as_agent(
            environment,
            command=command,
            env=env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )

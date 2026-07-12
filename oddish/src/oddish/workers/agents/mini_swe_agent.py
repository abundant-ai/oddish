from __future__ import annotations

import json
import os
import re
import shlex
import uuid

from harbor.agents.installed.mini_swe_agent import MiniSweAgent

from oddish.config import META_DEFAULT_BASE_URL, meta_bare_model_id, settings


_META_CONFIG_PATH = "/tmp/oddish-meta-mini-swe-agent.yaml"
_META_API_DOMAIN = "api.ai.meta.com"
# Matches the ``--task=<prompt> `` segment (up to the following --output=) that
# harbor's MiniSweAgent puts on the command line, so it can be extracted and
# stripped (the prompt is delivered via the config file instead).
_TASK_RE = re.compile(r"--task=(?P<task>.*?)\s+(?=--output=)", re.S)


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
        from harbor.environments.modal_network import normalize_domain_or_url

        candidates = {
            META_DEFAULT_BASE_URL,
            settings.meta_base_url,
            os.environ.get("META_BASE_URL"),
        }
        extra_env = (kwargs or {}).get("extra_env")
        if isinstance(extra_env, dict):
            candidates.update(
                {
                    extra_env.get("META_BASE_URL"),
                    extra_env.get("OPENAI_BASE_URL"),
                    extra_env.get("OPENAI_API_BASE"),
                }
            )

        domains = {
            domain
            for candidate in candidates
            if (domain := normalize_domain_or_url(candidate))
        }
        domains.add(_META_API_DOMAIN)
        return sorted(domains)

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

    async def _write_meta_config(
        self, environment, env: dict | None, task: str | None = None
    ) -> None:
        session_id = self._meta_session_id()
        quoted_path = shlex.quote(_META_CONFIG_PATH)
        heredoc_marker = f"ODDISH_META_MSWEA_CONFIG_{uuid.uuid4().hex[:8]}"
        config_yaml = (
            "model:\n"
            "  model_kwargs:\n"
            "    extra_headers:\n"
            f"      x-session-id: {json.dumps(session_id)}\n"
        )
        # Deliver the task via the config file (mini-swe-agent reads run.task,
        # see minisweagent.run.mini) instead of --task on the command line. The
        # agent frequently runs pkill -f <keyword> to restart servers it starts;
        # with the prompt on argv, those patterns match the mini-swe-agent
        # process's own cmdline and SIGTERM it (exit 143). Keeping the prompt off
        # argv avoids the self-kill. JSON is valid YAML, so it round-trips
        # multi-line prompts safely.
        if task is not None:
            config_yaml += f"run:\n  task: {json.dumps(task)}\n"
        command = (
            f"cat > {quoted_path} <<'{heredoc_marker}'\n{config_yaml}{heredoc_marker}\n"
        )
        await super().exec_as_agent(environment, command=command, env=env)

    def _patch_meta_command(self, command: str, *, strip_task: bool = False) -> str:
        model_name = self._litellm_model_name()
        if model_name:
            command = re.sub(
                r"--model=\S+",
                f"--model={shlex.quote(model_name)}",
                command,
                count=1,
            )

        # Drop --task=<prompt> from argv; the prompt now rides in the config file
        # (run.task) so the agent's own pkill -f can't match its cmdline.
        if strip_task:
            command = _TASK_RE.sub("", command, count=1)

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
            task = None
            m = _TASK_RE.search(command)
            if m:
                try:
                    task = shlex.split(m.group("task"))[0]
                except (ValueError, IndexError):
                    task = None
            await self._write_meta_config(environment, env, task=task)
            command = self._patch_meta_command(command, strip_task=task is not None)

        return await super().exec_as_agent(
            environment,
            command=command,
            env=env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )

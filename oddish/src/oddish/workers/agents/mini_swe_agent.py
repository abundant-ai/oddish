from __future__ import annotations

import json
import os
import re
import shlex
import uuid

from harbor.agents.installed.mini_swe_agent import MiniSweAgent

from oddish.config import META_DEFAULT_BASE_URL, meta_bare_model_id, settings
from oddish.workers.agents.network import normalize_domain_or_url


_CONFIG_PATH = "/tmp/oddish-mini-swe-agent.yaml"
_META_CONFIG_PATH = "/tmp/oddish-meta-mini-swe-agent.yaml"
_META_API_DOMAIN = "api.ai.meta.com"
_TASK_FLAG = "--task="
# litellm >= 1.92 imports its proxy/MCP handler chain (fastapi, orjson, ...) from
# completion() whenever ``tools`` is set, and mini-swe-agent always sends
# ``tools=[BASH_TOOL]`` (minisweagent.models.litellm_model). Those deps are not in
# the ``uv tool`` venv, so the first model call dies with ModuleNotFoundError and
# the trial fails at 2 steps / 0 tokens. Installing them is not an option: trials
# run with egress locked to the model API, so there is no PyPI to fetch from.
# litellm only uses that import to ask whether the tools are MCP-gateway tools --
# BASH_TOOL never is, so the branch falls through to the normal path regardless.
# Pass litellm's own ``_skip_mcp_handler`` escape hatch (main.py: ``skip_mcp_handler
# = kwargs.pop("_skip_mcp_handler", False)``) through mini-swe-agent's model_kwargs
# to skip the dead import. Behavior is unchanged; it just never needs orjson.
_SKIP_MCP_HANDLER_YAML = "    _skip_mcp_handler: true\n"


def _shell_token_end(s: str, start: int) -> int:
    """Index just past the shell token beginning at ``s[start]``.

    Honors single/double quotes and backslash escapes so the scan stops at the
    first *unquoted* whitespace. This makes ``--task=`` extraction robust even
    when the quoted prompt itself contains flag-looking text (e.g. ``--output=``)
    -- a naive regex up to the next ``--output=`` would truncate inside the
    prompt and leave ``--task=`` on argv.
    """
    i, n = start, len(s)
    squote = dquote = False
    while i < n:
        c = s[i]
        if squote:
            if c == "'":
                squote = False
        elif dquote:
            if c == "\\" and i + 1 < n:
                i += 1
            elif c == '"':
                dquote = False
        elif c == "'":
            squote = True
        elif c == '"':
            dquote = True
        elif c == "\\" and i + 1 < n:
            i += 1
        elif c.isspace():
            break
        i += 1
    return i


def _extract_task_span(command: str) -> tuple[str | None, tuple[int, int] | None]:
    """Return (task, (start, end)) for the ``--task=<quoted>`` segment, or (None, None).

    ``start`` includes a single preceding space when present so the whole
    segment can be excised cleanly.
    """
    idx = command.find(_TASK_FLAG)
    if idx < 0:
        return None, None
    vstart = idx + len(_TASK_FLAG)
    vend = _shell_token_end(command, vstart)
    try:
        task = shlex.split(command[vstart:vend])[0]
    except (ValueError, IndexError):
        return None, None
    start = idx - 1 if idx > 0 and command[idx - 1] == " " else idx
    return task, (start, vend)


def _slugify_session_part(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "oddish-eval"


class OddishMiniSweAgent(MiniSweAgent):
    _oddish_config_path = _CONFIG_PATH

    def _oddish_config_yaml(self, task: str | None) -> str:
        return "model:\n  model_kwargs:\n" + _SKIP_MCP_HANDLER_YAML

    def _oddish_take_task(self, command: str) -> tuple[str, str | None]:
        return command, None

    async def _oddish_write_config(
        self, environment, env: dict | None, task: str | None
    ) -> None:
        quoted_path = shlex.quote(self._oddish_config_path)
        heredoc_marker = f"ODDISH_MSWEA_CONFIG_{uuid.uuid4().hex[:8]}"
        command = (
            f"cat > {quoted_path} <<'{heredoc_marker}'\n"
            f"{self._oddish_config_yaml(task)}{heredoc_marker}\n"
        )
        await super().exec_as_agent(environment, command=command, env=env)

    def _oddish_patch_command(self, command: str) -> str:
        config_flags = f"-c {shlex.quote(self._oddish_config_path)} "
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
            command, task = self._oddish_take_task(command)
            await self._oddish_write_config(environment, env, task)
            command = self._oddish_patch_command(command)

        return await super().exec_as_agent(
            environment,
            command=command,
            env=env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )


class OddishMetaMiniSweAgent(OddishMiniSweAgent):
    """mini-swe-agent wrapper for Meta's OpenAI-compatible eval endpoint."""

    _oddish_config_path = _META_CONFIG_PATH

    @classmethod
    def required_outbound_domains(
        cls,
        model_name: str | None = None,
        kwargs: dict | None = None,
    ) -> list[str]:
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

    def _oddish_config_yaml(self, task: str | None) -> str:
        session_id = self._meta_session_id()
        config_yaml = (
            "model:\n"
            "  model_kwargs:\n"
            + _SKIP_MCP_HANDLER_YAML
            + "    extra_headers:\n"
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
        return config_yaml

    def _oddish_take_task(self, command: str) -> tuple[str, str | None]:
        # Excise --task=<prompt> from argv while its span is still valid;
        # _oddish_patch_command rewrites --model afterwards, which would shift offsets.
        task, task_span = _extract_task_span(command)
        if task_span is not None:
            start, end = task_span
            command = command[:start] + command[end:]
        return command, task

    def _oddish_patch_command(self, command: str) -> str:
        model_name = self._litellm_model_name()
        if model_name:
            command = re.sub(
                r"--model=\S+",
                f"--model={shlex.quote(model_name)}",
                command,
                count=1,
            )
        return super()._oddish_patch_command(command)

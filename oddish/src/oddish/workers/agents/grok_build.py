from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from typing import Any

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.grok_build import GrokBuild
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .grok_build_session import (
    GROK_SESSION_CAPTURE_DIRNAME,
    build_session_trajectory,
)
from .grok_build_trajectory import write_grok_build_trajectory_if_richer

from harbor.utils.trajectory_utils import format_trajectory_json


_OUTPUT_FILENAME = "grok-build.json"
_STDERR_FILENAME = "grok-build.stderr.log"

# Where the grok CLI persists its full session store (tool calls + token usage);
# the headless stdout does not carry these, so we copy this tree into the trial
# logs so it survives sandbox teardown and can be converted to a trajectory.
_SESSION_CAPTURE_PATH = f"/logs/agent/{GROK_SESSION_CAPTURE_DIRNAME}"

# Valid xAI transport backends the Grok CLI understands for a ``[model.*]``
# entry. Grok routes ``responses`` -> ``POST /v1/responses`` and
# ``chat_completions`` -> ``POST /v1/chat/completions``. The upstream Harbor
# ``GrokBuild`` hardcodes ``responses``, but not every xAI model is served on
# the Responses API: some (notably newer / unreleased models) are only exposed
# on Chat Completions and answer a Responses request with a 404
# ``The model <id> does not exist or your team does not have access to it``.
# Making the backend selectable lets a trial route such a model correctly
# without editing Harbor.
_VALID_API_BACKENDS = frozenset({"chat_completions", "responses", "messages"})
_API_BACKEND_RE = re.compile(r'api_backend = "[^"]*"')

# The rendered instruction is staged inside the sandbox as a file and read back
# via ``"$(cat ...)"`` instead of being inlined into the ``grok -p`` argv. Modal
# rejects any ``exec`` whose CMD arguments exceed 65536 bytes (ARG_MAX), and a
# large task instruction -- embedded up to three times across the CLI fallbacks
# -- blows past that limit and fails the whole trial during image/agent start.
# Uploading the prompt out-of-band keeps the exec command string tiny; the
# ``$(cat ...)`` substitution is expanded by the sandbox shell (bound only by
# the far larger in-sandbox Linux ARG_MAX), so grok still receives the full
# instruction as its ``-p`` argument.
_PROMPT_PATH = "/tmp/oddish-grok-build-prompt.txt"


class OddishGrokBuild(GrokBuild):
    """Grok Build wrapper that preserves streaming events for ATIF conversion."""

    def __init__(
        self,
        *args: Any,
        reasoning_effort: str | None = "high",
        api_backend: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.reasoning_effort = reasoning_effort
        normalized_backend = (api_backend or "").strip()
        if normalized_backend and normalized_backend not in _VALID_API_BACKENDS:
            raise ValueError(
                f"Unsupported grok-build api_backend {api_backend!r}; "
                f"expected one of {sorted(_VALID_API_BACKENDS)}."
            )
        self.api_backend = normalized_backend or None

    def build_config_toml(self) -> str:
        """Emit the grok config, honoring an ``api_backend`` override.

        Upstream Harbor pins every ``[model.*]`` block to
        ``api_backend = "responses"``. When ``api_backend`` is supplied (via an
        agent kwarg, e.g. ``--agent-kwarg api_backend=chat_completions``), swap
        the transport for every model entry so the trial can reach a model that
        is only served on that endpoint. When unset, the upstream default is
        preserved verbatim.
        """
        config: str = super().build_config_toml()
        if not self.api_backend:
            return config
        return _API_BACKEND_RE.sub(
            f"api_backend = {self._toml_string(self.api_backend)}", config
        )

    async def _stage_prompt(
        self, environment: BaseEnvironment, instruction: str
    ) -> None:
        """Upload the instruction into the sandbox as a readable file.

        ``upload_file`` transfers the bytes out-of-band (not via the exec argv),
        which is exactly what keeps us under Modal's ARG_MAX. It copies in as
        root, so we chmod the file world-readable afterwards to guarantee the
        (possibly non-root) agent user can read it back.
        """
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(instruction)
            tmp.flush()
            tmp.close()
            await environment.upload_file(tmp.name, _PROMPT_PATH)
        finally:
            os.unlink(tmp.name)

        await self.exec_as_root(
            environment,
            command=f"chmod 0644 {shlex.quote(_PROMPT_PATH)}",
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        await self._write_config(environment)
        await self._stage_prompt(environment, instruction)

        # Read the prompt back inside the sandbox rather than inlining it: the
        # command substitution is expanded by the sandbox shell, so the argv
        # sent to the Modal SDK stays small regardless of instruction size.
        prompt_arg = f'"$(cat {shlex.quote(_PROMPT_PATH)})"'
        stdout_path = f"/logs/agent/{_OUTPUT_FILENAME}"
        stderr_path = f"/logs/agent/{_STDERR_FILENAME}"

        def grok_command(
            output_format: str,
            *,
            no_auto_update: bool,
            include_reasoning_effort: bool = True,
        ) -> str:
            parts = [
                "grok",
                "-p",
                prompt_arg,
                "--always-approve",
                "--output-format",
                output_format,
            ]
            reasoning_effort = (self.reasoning_effort or "").strip()
            if include_reasoning_effort and reasoning_effort:
                parts.extend(["--reasoning-effort", shlex.quote(reasoning_effort)])
            if no_auto_update:
                parts.append("--no-auto-update")
            return " ".join(parts)

        reasoning_unsupported_pattern = "'(reasoning-effort|reasoning_effort)'"
        unsupported_pattern = (
            "'(streaming-json|output-format|no-auto-update|unknown option|"
            "unrecognized option|unexpected argument|invalid value|unsupported)'"
        )
        command = (
            "mkdir -p /logs/agent; "
            'export PATH="$HOME/.local/bin:$HOME/.grok/bin:$PATH"; '
            "set +e; "
            f"{grok_command('streaming-json', no_auto_update=True)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; "
            f"if [ $rc -ne 0 ] && grep -Eqi {reasoning_unsupported_pattern} {stderr_path}; then "
            f"{grok_command('streaming-json', no_auto_update=True, include_reasoning_effort=False)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; "
            "fi; "
            f"if [ $rc -ne 0 ] && grep -Eqi {unsupported_pattern} {stderr_path}; then "
            f"{grok_command('json', no_auto_update=True)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; "
            "fi; "
            f"if [ $rc -ne 0 ] && grep -Eqi {reasoning_unsupported_pattern} {stderr_path}; then "
            f"{grok_command('json', no_auto_update=True, include_reasoning_effort=False)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; "
            "fi; "
            "if [ $rc -ne 0 ] && grep -Eqi '(no-auto-update|unknown option|"
            f"unrecognized option|unexpected argument)' {stderr_path}; then "
            f"{grok_command('json', no_auto_update=False)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; "
            "fi; "
            f"if [ $rc -ne 0 ] && grep -Eqi {reasoning_unsupported_pattern} {stderr_path}; then "
            f"{grok_command('json', no_auto_update=False, include_reasoning_effort=False)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; "
            "fi; "
            "exit $rc"
        )
        await self.exec_as_agent(environment, command=command, env=self._xai_env())
        await self._capture_session(environment)

    async def _capture_session(self, environment: BaseEnvironment) -> None:
        """Copy the grok session store into the trial logs.

        The headless stdout only carries the assistant's text; the tool calls
        and per-turn token usage live under ``$GROK_HOME/sessions`` (and
        ``logs``). Copy that tree into ``/logs/agent/grok-session`` so it is
        uploaded with the trial and can be turned into a rich trajectory. Fully
        best-effort: a copy failure must never fail the trial.
        """
        capture = shlex.quote(_SESSION_CAPTURE_PATH)
        # Copy both sessions/ (tool-call trajectory) and logs/ (the sampling log,
        # which carries per-request token usage the session stream omits).
        command = (
            "set +e; "
            f"mkdir -p {capture}; "
            'GROK_HOME="${GROK_HOME:-$HOME/.grok}"; '
            f'cp -a "$GROK_HOME/sessions" {capture}/ 2>/dev/null; '
            f'cp -a "$GROK_HOME/logs" {capture}/ 2>/dev/null; '
            "exit 0"
        )
        try:
            await self.exec_as_agent(environment, command=command)
        except Exception:
            self.logger.warning("Failed to capture grok session store", exc_info=True)

    def _session_id_from_output(self) -> str | None:
        output_path = self.logs_dir / _OUTPUT_FILENAME
        if not output_path.is_file():
            return None
        try:
            text = output_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        session_id: str | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                candidate = event.get("sessionId") or event.get("session_id")
                if isinstance(candidate, str) and candidate:
                    session_id = candidate
        return session_id

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        trajectory = None
        # Prefer the on-disk session store (real tool calls + token usage) over
        # the text-only headless stdout.
        try:
            session_trajectory = build_session_trajectory(
                self.logs_dir / GROK_SESSION_CAPTURE_DIRNAME,
                session_id=self._session_id_from_output(),
                agent_version=getattr(self, "_version", None) or "unknown",
                model_name=self.model_name,
            )
        except Exception:
            self.logger.warning(
                "Failed to build Grok Build session trajectory", exc_info=True
            )
            session_trajectory = None

        if session_trajectory and session_trajectory.steps:
            try:
                (self.logs_dir / "trajectory.json").write_text(
                    format_trajectory_json(session_trajectory.to_json_dict()),
                    encoding="utf-8",
                )
                trajectory = session_trajectory
            except Exception:
                self.logger.warning(
                    "Failed to write Grok Build session trajectory", exc_info=True
                )

        if trajectory is None:
            try:
                trajectory = write_grok_build_trajectory_if_richer(
                    existing_trajectory_path=self.logs_dir / "trajectory.json",
                    output_path=self.logs_dir / _OUTPUT_FILENAME,
                    agent_version=getattr(self, "_version", None) or "unknown",
                    model_name=self.model_name,
                )
            except Exception:
                self.logger.exception("Failed to write Grok Build trajectory fallback")
                return

        if trajectory and trajectory.final_metrics:
            metrics = trajectory.final_metrics
            context.n_input_tokens = metrics.total_prompt_tokens or 0
            context.n_cache_tokens = metrics.total_cached_tokens or 0
            context.n_output_tokens = metrics.total_completion_tokens or 0
            context.cost_usd = metrics.total_cost_usd

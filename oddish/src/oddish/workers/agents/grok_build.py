from __future__ import annotations

import os
import shlex
import tempfile
from typing import override

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.grok_build import GrokBuild
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .grok_build_trajectory import write_grok_build_trajectory_if_richer


_OUTPUT_FILENAME = "grok-build.json"
_STDERR_FILENAME = "grok-build.stderr.log"

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

    @override
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

        def grok_command(output_format: str, *, no_auto_update: bool) -> str:
            parts = [
                "grok",
                "-p",
                prompt_arg,
                "--always-approve",
                "--output-format",
                output_format,
            ]
            if no_auto_update:
                parts.append("--no-auto-update")
            return " ".join(parts)

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
            f"if [ $rc -ne 0 ] && grep -Eqi {unsupported_pattern} {stderr_path}; then "
            f"{grok_command('json', no_auto_update=True)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; "
            "fi; "
            "if [ $rc -ne 0 ] && grep -Eqi '(no-auto-update|unknown option|"
            f"unrecognized option|unexpected argument)' {stderr_path}; then "
            f"{grok_command('json', no_auto_update=False)} "
            f">{stdout_path} 2>{stderr_path}; "
            "rc=$?; "
            "fi; "
            "exit $rc"
        )
        await self.exec_as_agent(environment, command=command, env=self._xai_env())

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
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

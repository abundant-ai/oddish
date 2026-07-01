from __future__ import annotations

import shlex
from typing import override

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.grok_build import GrokBuild
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .grok_build_trajectory import write_grok_build_trajectory_if_richer


_OUTPUT_FILENAME = "grok-build.json"
_STDERR_FILENAME = "grok-build.stderr.log"


class OddishGrokBuild(GrokBuild):
    """Grok Build wrapper that preserves streaming events for ATIF conversion."""

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        await self._write_config(environment)
        escaped_instruction = shlex.quote(instruction)
        stdout_path = f"/logs/agent/{_OUTPUT_FILENAME}"
        stderr_path = f"/logs/agent/{_STDERR_FILENAME}"

        def grok_command(output_format: str, *, no_auto_update: bool) -> str:
            parts = [
                "grok",
                "-p",
                escaped_instruction,
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

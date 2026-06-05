from __future__ import annotations

import json
import re
import shlex
from typing import Any

from harbor.agents.installed.codex import Codex
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.observation import Observation
from harbor.models.trajectories.observation_result import ObservationResult
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory
from harbor.utils.trajectory_utils import format_trajectory_json


_AZURE_CODEX_PROVIDER = "oddish_azure_openai"
_AZURE_CODEX_RETRY_CONFIG_PARAMS = {
    "text.verbosity": "model_verbosity",
}
_SUPPORTED_VALUES_RE = re.compile(
    r"Supported values are:\s*(?P<values>(?:'[^']+'\s*,?\s*)+)"
)
_CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT = 20_000


def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _truncate_trajectory_text(value: Any, *, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + " ... [truncated]"


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

    def _read_existing_trajectory_step_count(self) -> int:
        trajectory_path = self.logs_dir / "trajectory.json"
        try:
            data = json.loads(trajectory_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        steps = data.get("steps")
        return len(steps) if isinstance(steps, list) else 0

    def _convert_stdout_events_to_trajectory(self) -> Trajectory | None:
        """Convert ``codex exec --json`` stdout into ATIF when sessions are sparse.

        Current Codex CLI emits rich JSONL events to stdout. Harbor's bundled
        Codex converter reads ``$CODEX_HOME/sessions`` and can miss those events
        when the session schema changes, leaving Oddish with a nearly empty
        Trajectory tab even though ``agent/codex.txt`` has the full run.
        """
        output_path = self.logs_dir / self._OUTPUT_FILENAME
        if not output_path.is_file():
            return None

        events: list[dict[str, Any]] = []
        try:
            with output_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped or not stripped.startswith("{"):
                        continue
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        events.append(parsed)
        except OSError:
            return None

        if not events:
            return None

        thread_id = None
        steps: list[Step] = []
        completed_items: set[str] = set()

        for event in events:
            etype = event.get("type")
            if etype == "thread.started":
                thread_id = event.get("thread_id")
                continue

            if etype == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or f"step-{len(steps) + 1}")
                completed_items.add(item_id)
                item_type = item.get("type")

                if item_type == "command_execution":
                    command = str(item.get("command") or "")
                    output = _truncate_trajectory_text(
                        item.get("aggregated_output"),
                        limit=_CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT,
                    )
                    steps.append(
                        Step(
                            step_id=len(steps) + 1,
                            source="agent",
                            model_name=self.model_name,
                            message=f"Executed command: {command}",
                            tool_calls=[
                                ToolCall(
                                    tool_call_id=item_id,
                                    function_name="shell",
                                    arguments={"command": command},
                                )
                            ],
                            observation=Observation(
                                results=[
                                    ObservationResult(
                                        source_call_id=item_id,
                                        content=output,
                                    )
                                ]
                            ),
                            extra={
                                "status": item.get("status"),
                                "exit_code": item.get("exit_code"),
                                "source": "codex_stdout_jsonl",
                            },
                        )
                    )
                    continue

                if item_type == "reasoning":
                    text = _truncate_trajectory_text(
                        item.get("text"),
                        limit=_CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT,
                    )
                    steps.append(
                        Step(
                            step_id=len(steps) + 1,
                            source="agent",
                            model_name=self.model_name,
                            message="Reasoning",
                            reasoning_content=text,
                            extra={"source": "codex_stdout_jsonl"},
                        )
                    )
                    continue

                if item_type == "agent_message":
                    text = _truncate_trajectory_text(
                        item.get("text"),
                        limit=_CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT,
                    )
                    steps.append(
                        Step(
                            step_id=len(steps) + 1,
                            source="agent",
                            model_name=self.model_name,
                            message=text,
                            extra={"source": "codex_stdout_jsonl"},
                        )
                    )
                    continue

            if etype == "item.started":
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or "")
                if not item_id or item_id in completed_items:
                    continue
                if item.get("type") != "command_execution":
                    continue
                command = str(item.get("command") or "")
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="agent",
                        model_name=self.model_name,
                        message=f"Started command: {command}",
                        tool_calls=[
                            ToolCall(
                                tool_call_id=item_id,
                                function_name="shell",
                                arguments={"command": command},
                            )
                        ],
                        extra={
                            "status": item.get("status"),
                            "source": "codex_stdout_jsonl",
                        },
                    )
                )

            if etype == "turn.failed":
                error = event.get("error")
                message = (
                    error.get("message")
                    if isinstance(error, dict)
                    else event.get("message")
                )
                if message:
                    steps.append(
                        Step(
                            step_id=len(steps) + 1,
                            source="agent",
                            model_name=self.model_name,
                            message=_truncate_trajectory_text(
                                message,
                                limit=_CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT,
                            ),
                            extra={"source": "codex_stdout_jsonl", "type": etype},
                        )
                    )

        if not steps:
            return None

        final_metrics = None
        for event in reversed(events):
            if event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            prompt_tokens = usage.get("input_tokens")
            completion_tokens = usage.get("output_tokens")
            cached_tokens = usage.get("cached_input_tokens")
            final_metrics = FinalMetrics(
                total_prompt_tokens=prompt_tokens if prompt_tokens else None,
                total_completion_tokens=completion_tokens or None,
                total_cached_tokens=cached_tokens or None,
                total_cost_usd=self._compute_cost_from_pricing(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_tokens=cached_tokens,
                ),
                total_steps=len(steps),
                extra={
                    "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "source": "codex_stdout_jsonl",
                },
            )
            break

        return Trajectory(
            schema_version="ATIF-v1.5",
            session_id=str(thread_id) if thread_id else None,
            agent=Agent(
                name="codex",
                version=self._version or "unknown",
                model_name=self.model_name,
                extra={
                    "originator": "codex_exec",
                    "trajectory_source": "codex_stdout_jsonl",
                },
            ),
            steps=steps,
            final_metrics=final_metrics,
        )

    def _write_stdout_trajectory_if_richer(self, context) -> None:
        trajectory = self._convert_stdout_events_to_trajectory()
        if not trajectory:
            return

        if len(trajectory.steps) <= self._read_existing_trajectory_step_count():
            return

        trajectory_path = self.logs_dir / "trajectory.json"
        try:
            trajectory_path.write_text(
                format_trajectory_json(trajectory.to_json_dict()),
                encoding="utf-8",
            )
        except OSError:
            self.logger.exception("Failed to write Codex stdout trajectory fallback")
            return

        if trajectory.final_metrics:
            metrics = trajectory.final_metrics
            context.cost_usd = metrics.total_cost_usd
            context.n_input_tokens = metrics.total_prompt_tokens or 0
            context.n_cache_tokens = metrics.total_cached_tokens or 0
            context.n_output_tokens = metrics.total_completion_tokens or 0

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

    def populate_context_post_run(self, context) -> None:
        super().populate_context_post_run(context)
        self._write_stdout_trajectory_if_richer(context)

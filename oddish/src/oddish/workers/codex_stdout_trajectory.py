from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.observation import Observation
from harbor.models.trajectories.observation_result import ObservationResult
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory
from harbor.utils.trajectory_utils import format_trajectory_json

CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT = 20_000


def _truncate_trajectory_text(value: Any, *, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + " ... [truncated]"


def read_trajectory_step_count(trajectory_path: Path) -> int:
    try:
        data = json.loads(trajectory_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    steps = data.get("steps")
    return len(steps) if isinstance(steps, list) else 0


def convert_codex_stdout_jsonl_to_trajectory(
    output_path: Path,
    *,
    agent_version: str,
    model_name: str | None,
    compute_cost: Callable[..., float | None] | None = None,
) -> Trajectory | None:
    """Convert ``codex exec --json`` stdout into ATIF.

    Codex CLI's stdout JSONL is the stable artifact Oddish captures as
    ``agent/codex.txt``. Some Codex releases write sparse session JSONL files,
    so this converter lets workers recover a useful trajectory without
    depending on session-internal schemas.
    """
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
                    limit=CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT,
                )
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="agent",
                        model_name=model_name,
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
                    limit=CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT,
                )
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="agent",
                        model_name=model_name,
                        message="Reasoning",
                        reasoning_content=text,
                        extra={"source": "codex_stdout_jsonl"},
                    )
                )
                continue

            if item_type == "agent_message":
                text = _truncate_trajectory_text(
                    item.get("text"),
                    limit=CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT,
                )
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="agent",
                        model_name=model_name,
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
                    model_name=model_name,
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
                        model_name=model_name,
                        message=_truncate_trajectory_text(
                            message,
                            limit=CODEX_STDOUT_TRAJECTORY_OUTPUT_LIMIT,
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
        total_cost_usd = (
            compute_cost(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
            )
            if compute_cost
            else None
        )
        final_metrics = FinalMetrics(
            total_prompt_tokens=prompt_tokens if prompt_tokens else None,
            total_completion_tokens=completion_tokens or None,
            total_cached_tokens=cached_tokens or None,
            total_cost_usd=total_cost_usd,
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
            version=agent_version,
            model_name=model_name,
            extra={
                "originator": "codex_exec",
                "trajectory_source": "codex_stdout_jsonl",
            },
        ),
        steps=steps,
        final_metrics=final_metrics,
    )


def write_trajectory_if_richer(
    *,
    existing_trajectory_path: Path,
    output_path: Path,
    agent_version: str,
    model_name: str | None,
    compute_cost: Callable[..., float | None] | None = None,
) -> Trajectory | None:
    trajectory = convert_codex_stdout_jsonl_to_trajectory(
        output_path,
        agent_version=agent_version,
        model_name=model_name,
        compute_cost=compute_cost,
    )
    if not trajectory:
        return None

    if len(trajectory.steps) <= read_trajectory_step_count(existing_trajectory_path):
        return None

    existing_trajectory_path.write_text(
        format_trajectory_json(trajectory.to_json_dict()),
        encoding="utf-8",
    )
    return trajectory

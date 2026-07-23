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


class _SessionTimeline:
    """Per-kind timestamps from the Codex session rollout JSONL.

    ``codex exec --json`` stdout events carry no timestamps, but the session
    rollout records one per entry, and both logs list shell calls, reasoning,
    and assistant messages chronologically — so the k-th stdout item of a kind
    aligns with the k-th rollout entry of that kind. There is no shared id to
    join on, so a kind is used only when the two logs agree on its count;
    schema drift degrades to missing timestamps, never wrong ones.
    """

    def __init__(self) -> None:
        self.shell_calls: list[tuple[str | None, str | None]] = []  # (ts, call_id)
        self.output_ts: dict[str, str] = {}  # call_id -> ts
        self.reasoning_ts: list[str | None] = []
        self.message_ts: list[str | None] = []

    def command_ts(self, k: int, *, completed: bool) -> str | None:
        if k >= len(self.shell_calls):
            return None
        ts, call_id = self.shell_calls[k]
        if completed and call_id and call_id in self.output_ts:
            return self.output_ts[call_id]
        return ts


def _read_session_timeline(
    sessions_dir: Path | None, thread_id: str | None
) -> _SessionTimeline | None:
    if sessions_dir is None or not sessions_dir.is_dir():
        return None

    candidates = sorted(sessions_dir.rglob("rollout-*.jsonl"))
    if not candidates:
        return None

    chosen: Path | None = None
    for path in candidates:
        meta_id = _session_file_id(path)
        if thread_id and meta_id == thread_id:
            chosen = path
            break
    if chosen is None:
        if len(candidates) != 1:
            return None
        chosen = candidates[0]

    timeline = _SessionTimeline()
    try:
        with chosen.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "response_item":
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                timestamp = entry.get("timestamp")
                if timestamp is not None and not isinstance(timestamp, str):
                    timestamp = None
                ptype = payload.get("type")
                if ptype == "function_call" and "shell" in str(payload.get("name", "")):
                    call_id = payload.get("call_id")
                    timeline.shell_calls.append(
                        (timestamp, call_id if isinstance(call_id, str) else None)
                    )
                elif ptype == "local_shell_call":
                    call_id = payload.get("call_id")
                    timeline.shell_calls.append(
                        (timestamp, call_id if isinstance(call_id, str) else None)
                    )
                elif (
                    ptype in ("function_call_output", "local_shell_call_output")
                    and timestamp
                ):
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str):
                        timeline.output_ts[call_id] = timestamp
                elif ptype == "reasoning":
                    timeline.reasoning_ts.append(timestamp)
                elif ptype == "message" and payload.get("role") == "assistant":
                    timeline.message_ts.append(timestamp)
    except OSError:
        return None
    return timeline


def _session_file_id(path: Path) -> str | None:
    """Session id from a rollout file's leading ``session_meta`` entry."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(5):
                line = handle.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("type") == "session_meta":
                    payload = entry.get("payload")
                    if isinstance(payload, dict):
                        meta_id = payload.get("session_id") or payload.get("id")
                        return str(meta_id) if meta_id else None
    except OSError:
        return None
    return None


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
    sessions_dir: Path | None = None,
) -> Trajectory | None:
    """Convert ``codex exec --json`` stdout into ATIF.

    Codex CLI's stdout JSONL is the stable artifact Oddish captures as
    ``agent/codex.txt``. Some Codex releases write sparse session JSONL files,
    so this converter lets workers recover a useful trajectory without
    depending on session-internal schemas.

    Stdout events carry no timestamps; when ``sessions_dir`` holds the run's
    rollout JSONL, steps are stamped from it by ordinal alignment (see
    ``_SessionTimeline``) so per-step timing survives this fallback path.
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

    # Alignment bookkeeping for session-timeline timestamps: one meta entry per
    # step — ("command", ordinal, completed) / ("reasoning", k) / ("message", k)
    # / None — where command ordinals are per distinct item in first-appearance
    # order, matching the rollout's chronological shell-call order.
    step_meta: list[tuple[Any, ...] | None] = []
    command_ordinals: dict[str, int] = {}
    reasoning_count = 0
    message_count = 0

    def command_ordinal(item_id: str) -> int:
        if item_id not in command_ordinals:
            command_ordinals[item_id] = len(command_ordinals)
        return command_ordinals[item_id]

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
                step_meta.append(("command", command_ordinal(item_id), True))
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
                step_meta.append(("reasoning", reasoning_count))
                reasoning_count += 1
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
                step_meta.append(("message", message_count))
                message_count += 1
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
            step_meta.append(("command", command_ordinal(item_id), False))

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
                step_meta.append(None)

    if not steps:
        return None

    timeline = _read_session_timeline(
        sessions_dir, str(thread_id) if thread_id else None
    )
    if timeline is not None:
        use_commands = len(command_ordinals) == len(timeline.shell_calls)
        use_reasoning = reasoning_count == len(timeline.reasoning_ts)
        use_messages = message_count == len(timeline.message_ts)
        for step, meta in zip(steps, step_meta):
            if meta is None:
                continue
            if meta[0] == "command" and use_commands:
                step.timestamp = timeline.command_ts(meta[1], completed=meta[2])
            elif meta[0] == "reasoning" and use_reasoning:
                step.timestamp = timeline.reasoning_ts[meta[1]]
            elif meta[0] == "message" and use_messages:
                step.timestamp = timeline.message_ts[meta[1]]

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
    sessions_dir: Path | None = None,
) -> Trajectory | None:
    trajectory = convert_codex_stdout_jsonl_to_trajectory(
        output_path,
        agent_version=agent_version,
        model_name=model_name,
        compute_cost=compute_cost,
        sessions_dir=sessions_dir,
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

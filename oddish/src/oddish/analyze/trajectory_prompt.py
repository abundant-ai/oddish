"""Bound trajectory text before it is sent to a summary model."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

MAX_TEXT_CHARS = 2_000
TRUNCATE_HEAD = 800
TRUNCATE_TAIL = 400
MAX_TRAJECTORY_CHARS = 400_000
STEP_OMISSION_MARKER = "[{n} steps omitted to fit the context window]"
_ROOT_PROMPT_FIELDS = ("schema_version", "session_id", "agent", "final_metrics")


def compact_summary_text(value: str | None) -> str:
    """Keep enough of a large field to show both its setup and outcome."""
    if value is None:
        return "[unavailable]"
    if len(value) <= MAX_TEXT_CHARS:
        return value
    omitted = len(value) - TRUNCATE_HEAD - TRUNCATE_TAIL
    return (
        value[:TRUNCATE_HEAD]
        + f"\n[...truncated {omitted} chars...]\n"
        + value[-TRUNCATE_TAIL:]
    )


def _process_content(value: Any) -> Any:
    if isinstance(value, str):
        return compact_summary_text(value)
    if not isinstance(value, list):
        return value

    output: list[Any] = []
    omitted_images = 0
    for part in value:
        if isinstance(part, dict) and part.get("type") == "image":
            omitted_images += 1
        elif isinstance(part, dict) and part.get("type") == "text":
            output.append(
                {**part, "text": compact_summary_text(str(part.get("text") or ""))}
            )
        else:
            output.append(part)
    if omitted_images:
        output.append({"type": "text", "text": f"[image omitted] (x{omitted_images})"})
    return output


def _has_content(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(
            _has_content(part.get("text"))
            if isinstance(part, dict) and part.get("type") == "text"
            else bool(part)
            for part in value
        )
    return bool(value)


def _step_is_inert(step: dict[str, Any]) -> bool:
    if step.get("tool_calls"):
        return False
    if _has_content(step.get("message")) or _has_content(step.get("reasoning_content")):
        return False
    observation = step.get("observation")
    if not isinstance(observation, dict):
        return not bool(observation)
    return not any(
        _has_content(result.get("content"))
        if isinstance(result, dict)
        else bool(result)
        for result in observation.get("results") or []
    )


def _compact_step(step: dict[str, Any]) -> dict[str, Any]:
    output = deepcopy(step)
    output["message"] = _process_content(step.get("message"))
    reasoning = step.get("reasoning_content")
    if isinstance(reasoning, str):
        output["reasoning_content"] = compact_summary_text(reasoning)

    tool_calls = []
    for call in step.get("tool_calls") or []:
        if not isinstance(call, dict):
            tool_calls.append(call)
            continue
        compact_call = dict(call)
        arguments = call.get("arguments")
        if isinstance(arguments, dict):
            compact_call["arguments"] = {
                key: compact_summary_text(value) if isinstance(value, str) else value
                for key, value in arguments.items()
            }
        tool_calls.append(compact_call)
    output["tool_calls"] = tool_calls or None

    observation = step.get("observation")
    if isinstance(observation, dict):
        compact_observation = dict(observation)
        compact_observation["results"] = [
            {**result, "content": _process_content(result.get("content"))}
            if isinstance(result, dict)
            else result
            for result in observation.get("results") or []
        ]
        output["observation"] = compact_observation
    return output


def _clip_steps(trajectory: dict[str, Any], max_steps: int) -> dict[str, Any]:
    steps = trajectory.get("steps") or []
    if len(steps) <= max_steps:
        return trajectory
    head = max_steps // 2
    tail = max_steps - head
    last_dropped = steps[len(steps) - tail - 1]
    marker = {
        "step_id": None,
        "source": "system",
        "message": STEP_OMISSION_MARKER.format(n=len(steps) - max_steps),
        "timestamp": (
            last_dropped.get("timestamp") if isinstance(last_dropped, dict) else None
        ),
    }
    return {**trajectory, "steps": [*steps[:head], marker, *steps[-tail:]]}


def compact_trajectory_for_prompt(
    trajectory: dict[str, Any], *, max_chars: int = MAX_TRAJECTORY_CHARS
) -> dict[str, Any]:
    """Remove empty turns and shrink the middle until serialized input is bounded."""
    root = {
        key: deepcopy(trajectory[key])
        for key in _ROOT_PROMPT_FIELDS
        if key in trajectory
    }
    root["steps"] = [
        _compact_step(step)
        for step in trajectory.get("steps") or []
        if isinstance(step, dict) and not _step_is_inert(step)
    ]
    candidate = root
    step_budget = len(root["steps"])
    while (
        len(json.dumps(candidate, ensure_ascii=False)) > max_chars and step_budget > 1
    ):
        step_budget = max(1, step_budget // 2)
        candidate = _clip_steps(root, step_budget)
    serialized_chars = len(json.dumps(candidate, ensure_ascii=False))
    if serialized_chars > max_chars:
        raise ValueError(
            "one compacted trajectory step exceeds the summary prompt limit: "
            f"{serialized_chars} > {max_chars} characters"
        )
    return candidate

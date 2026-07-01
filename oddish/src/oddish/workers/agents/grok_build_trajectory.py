from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.trajectory import Trajectory
from harbor.utils.trajectory_utils import format_trajectory_json


GROK_BUILD_TRAJECTORY_CHUNK_LIMIT = 2_500


def _read_trajectory_step_count(trajectory_path: Path) -> int:
    try:
        data = json.loads(trajectory_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    steps = data.get("steps")
    return len(steps) if isinstance(steps, list) else 0


def parse_grok_build_payloads(text: str) -> list[dict[str, Any]]:
    """Parse Grok Build's streamed JSON/NDJSON artifact."""
    stripped = text.strip()
    if not stripped:
        return []

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]

    payloads: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            payloads.append(item)
    return payloads


def _event_text(event: dict[str, Any]) -> str:
    for key in ("data", "text", "content", "message"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    return ""


def _first_string(payloads: list[dict[str, Any]], *keys: str) -> str | None:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _split_text_chunks(
    text: str,
    *,
    limit: int = GROK_BUILD_TRAJECTORY_CHUNK_LIMIT,
) -> list[str]:
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        break_at = -1
        for pattern in (r"\n\n", r"\n", r"(?<=[.!?])\s+", r"(?<=[.!?])(?=[A-Z`])"):
            matches = list(re.finditer(pattern, window))
            if matches:
                candidate = matches[-1].end()
                if candidate >= limit // 3:
                    break_at = candidate
                    break
        if break_at <= 0:
            break_at = limit
        chunks.append(rest[:break_at].strip())
        rest = rest[break_at:].strip()
    if rest:
        chunks.append(rest)
    return [chunk for chunk in chunks if chunk]


def convert_grok_build_payloads_to_trajectory(
    payloads: list[dict[str, Any]],
    *,
    agent_version: str,
    model_name: str | None,
) -> Trajectory | None:
    if not payloads:
        return None

    session_id = _first_string(
        payloads,
        "session_id",
        "sessionId",
        "conversation_id",
        "conversationId",
    )
    resolved_model = _first_string(payloads, "model", "model_name", "modelName")
    resolved_version = _first_string(payloads, "version", "cli_version", "cliVersion")

    steps: list[Step] = []
    current_type: str | None = None
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_type, current_parts
        if not current_type or not current_parts:
            current_type = None
            current_parts = []
            return
        text = "".join(current_parts)
        chunks = _split_text_chunks(text)
        total_chunks = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            extra: dict[str, object] = {
                "source": "grok_build_json",
                "type": current_type,
            }
            if total_chunks > 1:
                extra["part"] = index
                extra["parts"] = total_chunks
            if current_type == "thought":
                message = "Reasoning"
                if total_chunks > 1:
                    message = f"Reasoning ({index}/{total_chunks})"
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="agent",
                        model_name=resolved_model or model_name,
                        message=message,
                        reasoning_content=chunk,
                        llm_call_count=1,
                        extra=extra,
                    )
                )
            else:
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="agent",
                        model_name=resolved_model or model_name,
                        message=chunk,
                        llm_call_count=1,
                        extra=extra,
                    )
                )
        current_type = None
        current_parts = []

    for payload in payloads:
        event_type = str(payload.get("type") or payload.get("kind") or "").lower()
        if event_type in {"thought", "thinking", "reasoning"}:
            if current_type != "thought":
                flush()
                current_type = "thought"
            current_parts.append(_event_text(payload))
            continue
        if event_type in {"text", "message", "output"}:
            if current_type != "text":
                flush()
                current_type = "text"
            current_parts.append(_event_text(payload))
            continue
        if event_type == "end":
            session_id = session_id or _first_string(
                [payload], "sessionId", "session_id"
            )
            continue

        flush()
        message = _event_text(payload)
        if not message:
            continue
        steps.append(
            Step(
                step_id=len(steps) + 1,
                source="agent",
                model_name=resolved_model or model_name,
                message=message,
                llm_call_count=1,
                extra={"source": "grok_build_json", "type": event_type or "event"},
            )
        )

    flush()
    if not steps:
        return None

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        agent=Agent(
            name="grok-build",
            version=resolved_version or agent_version,
            model_name=resolved_model or model_name,
            extra={"trajectory_source": "grok_build_json"},
        ),
        steps=steps,
        final_metrics=FinalMetrics(
            total_steps=len(steps),
            extra={"source": "grok_build_json", "raw_payload_count": len(payloads)},
        ),
        extra={"raw_payload_count": len(payloads)},
    )


def convert_grok_build_json_text_to_trajectory(
    text: str,
    *,
    agent_version: str,
    model_name: str | None,
) -> Trajectory | None:
    return convert_grok_build_payloads_to_trajectory(
        parse_grok_build_payloads(text),
        agent_version=agent_version,
        model_name=model_name,
    )


def convert_grok_build_json_to_trajectory(
    output_path: Path,
    *,
    agent_version: str,
    model_name: str | None,
) -> Trajectory | None:
    if not output_path.is_file():
        return None
    try:
        text = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return convert_grok_build_json_text_to_trajectory(
        text,
        agent_version=agent_version,
        model_name=model_name,
    )


def write_grok_build_trajectory_if_richer(
    *,
    existing_trajectory_path: Path,
    output_path: Path,
    agent_version: str,
    model_name: str | None,
) -> Trajectory | None:
    trajectory = convert_grok_build_json_to_trajectory(
        output_path,
        agent_version=agent_version,
        model_name=model_name,
    )
    if not trajectory:
        return None
    if len(trajectory.steps) <= _read_trajectory_step_count(existing_trajectory_path):
        return None

    existing_trajectory_path.write_text(
        format_trajectory_json(trajectory.to_json_dict()),
        encoding="utf-8",
    )
    return trajectory

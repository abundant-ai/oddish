from __future__ import annotations

from collections.abc import Mapping
from typing import Any

BROWSE_STATUS_KEYS = (
    "pass",
    "partial",
    "fail",
    "harness-error",
    "scoreless",
    "skipped",
    "pending",
    "queued",
    "running",
)


def status_value(value: Any) -> str:
    return str(getattr(value, "value", value)).rsplit(".", 1)[-1].lower()


def matrix_status(row: Mapping[str, Any]) -> str:
    status = status_value(row["status"])
    reward = row["reward"]
    error = row["error_message"]
    timeout = bool(error) and (
        "AgentTimeoutError" in error or "Agent execution timed out" in error
    )
    if status == "skipped":
        return "skipped"
    if error and not (timeout and reward is not None):
        return "harness-error"
    if status == "failed":
        return (
            reward_status(reward) if timeout and reward is not None else "harness-error"
        )
    if status == "success":
        return reward_status(reward) if reward is not None else "scoreless"
    if status in {"queued", "pending"}:
        return "queued"
    return "running" if status == "running" else "pending"


def reward_status(reward: float) -> str:
    if reward == 1:
        return "pass"
    return "fail" if reward == 0 else "partial"

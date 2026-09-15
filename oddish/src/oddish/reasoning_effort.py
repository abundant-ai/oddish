"""Read explicitly configured effort without inventing historical defaults."""

import re

from collections.abc import Mapping
from typing import Any

from harbor.models.trial.config import AgentConfig

from sqlalchemy import String, Text, case, cast, func
from sqlalchemy.dialects.postgresql import ARRAY


def normalize_reasoning_effort(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().lower() or None


def configured_reasoning_effort(config: Mapping[str, Any] | None) -> str | None:
    value = None
    for key in ("agent_overrides", "agent_config"):
        block = (config or {}).get(key)
        kwargs = block.get("kwargs") if isinstance(block, Mapping) else None
        if isinstance(kwargs, Mapping) and "reasoning_effort" in kwargs:
            value = kwargs["reasoning_effort"]
    return normalize_reasoning_effort(value)


def reasoning_effort_expression(config: Any) -> Any:
    # JSON null intentionally wins over the legacy value, just as dict merging
    # in the worker does. SQL NULL (a missing key) falls through to legacy.
    value = func.coalesce(
        config["agent_config"]["kwargs"]["reasoning_effort"],
        config["agent_overrides"]["kwargs"]["reasoning_effort"],
    )
    return cast(
        case(
            (
                func.jsonb_typeof(value) == "string",
                func.nullif(
                    func.lower(func.btrim(value.op("#>>")(cast([], ARRAY(Text))))), ""
                ),
            ),
            else_=None,
        ),
        String,
    )


def with_default_reasoning_effort(
    agent: str, model: str | None, config: AgentConfig | None
) -> AgentConfig | None:
    """Pin new submissions to high without changing explicit settings or history."""
    kwargs = config.kwargs if config else {}
    if "reasoning_effort" in kwargs or (config and config.import_path):
        return config
    agent = agent.strip().lower()
    model = (model or "").strip().lower()
    if not model:
        return config
    env_key = {
        "claude-code": "CLAUDE_CODE_EFFORT_LEVEL",
        "grok-build": "GROK_BUILD_REASONING_EFFORT",
        "tbh": "TBH_REASONING_EFFORT",
    }.get(agent)
    if env_key and config and env_key in config.env:
        return config

    supported = agent in {"grok-build", "copilot-cli", "dsh", "tbh"}
    if agent == "cursor-cli":
        supported = not re.search(r"\[(?:[^\]]*,)?\s*effort\s*=", model)
    model_routed = agent in {"mini-swe-agent", "aider", "openhands"}
    if agent in {"gemini-cli", "antigravity-cli"} or model_routed:
        supported = bool(re.search(r"(?:^|/)gemini-3(?:[.-]|$)", model))
    if agent == "claude-code" or model_routed:
        supported |= bool(re.search(r"claude-(opus|sonnet)-", model))
    if agent == "codex" or model_routed:
        supported |= bool(re.search(r"(?:^|/)(?:gpt-5|o[134](?:-|$))", model))
    if not supported:
        return config
    values = {**kwargs, "reasoning_effort": "high"}
    return (
        config.model_copy(update={"kwargs": values})
        if config
        else AgentConfig(kwargs=values)
    )

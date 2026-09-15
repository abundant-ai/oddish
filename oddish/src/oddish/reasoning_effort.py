"""Read explicitly configured effort without inventing historical defaults."""

from collections.abc import Mapping
from typing import Any

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

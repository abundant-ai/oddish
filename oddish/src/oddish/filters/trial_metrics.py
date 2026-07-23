"""Dependency-light trial metric filter contract.

This module intentionally does not import SQLAlchemy so the base CLI install can
parse and serialize the exact same contract as server installations.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator


class TrialMetricMatch(StrEnum):
    ANY = "any"
    ALL = "all"


def _csv(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    values = value.split(",") if isinstance(value, str) else value
    return list(
        dict.fromkeys(str(item).strip() for item in values if str(item).strip())
    )


def _tool_minimums(value: str | Mapping[str, Any] | None) -> dict[str, int]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("tool_count_mins must be a JSON object") from exc
    if not isinstance(value, Mapping):
        raise ValueError("tool_count_mins must be a JSON object")
    parsed: dict[str, int] = {}
    for raw_name, raw_minimum in value.items():
        name = str(raw_name).strip()
        if not name:
            raise ValueError("tool_count_mins contains an empty tool name")
        try:
            minimum = int(raw_minimum)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"minimum for tool {name!r} must be an integer") from exc
        if minimum < 0:
            raise ValueError(f"minimum for tool {name!r} must be non-negative")
        parsed[name] = minimum
    return parsed


class TrialMetricFilter(BaseModel):
    """Filters evaluated together against one eligible trial population."""

    models: list[str] = Field(default_factory=list)
    min_steps: int | None = Field(None, ge=0)
    max_steps: int | None = Field(None, ge=0)
    min_duration_seconds: float | None = Field(None, ge=0)
    max_duration_seconds: float | None = Field(None, ge=0)
    min_tool_calls: int | None = Field(None, ge=0)
    max_tool_calls: int | None = Field(None, ge=0)
    tool_names: list[str] = Field(default_factory=list)
    tool_count_mins: dict[str, int] = Field(default_factory=dict)
    match: TrialMetricMatch = TrialMetricMatch.ANY

    @field_validator("models", "tool_names", mode="before")
    @classmethod
    def parse_csv_fields(cls, value: Any) -> list[str]:
        return _csv(value)

    @field_validator("tool_count_mins", mode="before")
    @classmethod
    def parse_tool_minimums(cls, value: Any) -> dict[str, int]:
        return _tool_minimums(value)

    @model_validator(mode="after")
    def validate_bounds(self) -> "TrialMetricFilter":
        for label, minimum, maximum in (
            ("steps", self.min_steps, self.max_steps),
            ("duration", self.min_duration_seconds, self.max_duration_seconds),
            ("tool calls", self.min_tool_calls, self.max_tool_calls),
        ):
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError(f"minimum {label} cannot exceed maximum {label}")
        return self

    @classmethod
    def from_query(
        cls,
        *,
        models: str | Sequence[str] | None = None,
        min_steps: int | None = None,
        max_steps: int | None = None,
        min_duration_seconds: float | None = None,
        max_duration_seconds: float | None = None,
        min_tool_calls: int | None = None,
        max_tool_calls: int | None = None,
        tool_names: str | Sequence[str] | None = None,
        tool_count_mins: str | Mapping[str, Any] | None = None,
        match: str | TrialMetricMatch = TrialMetricMatch.ANY,
    ) -> "TrialMetricFilter":
        return cls(
            models=models,
            min_steps=min_steps,
            max_steps=max_steps,
            min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds,
            min_tool_calls=min_tool_calls,
            max_tool_calls=max_tool_calls,
            tool_names=tool_names,
            tool_count_mins=tool_count_mins,
            match=match,
        )

    @property
    def has_metric_constraints(self) -> bool:
        return (
            any(
                value is not None
                for value in (
                    self.min_steps,
                    self.max_steps,
                    self.min_duration_seconds,
                    self.max_duration_seconds,
                    self.min_tool_calls,
                    self.max_tool_calls,
                )
            )
            or bool(self.tool_names)
            or bool(self.tool_count_mins)
        )

    @property
    def is_empty(self) -> bool:
        return not self.models and not self.has_metric_constraints

    def to_query_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.models:
            params["models"] = ",".join(self.models)
        for key in (
            "min_steps",
            "max_steps",
            "min_duration_seconds",
            "max_duration_seconds",
            "min_tool_calls",
            "max_tool_calls",
        ):
            value = getattr(self, key)
            if value is not None:
                params[key] = str(value)
        if self.tool_names:
            params["tool_names"] = ",".join(self.tool_names)
        if self.tool_count_mins:
            params["tool_count_mins"] = json.dumps(
                self.tool_count_mins, sort_keys=True, separators=(",", ":")
            )
        if self.match is TrialMetricMatch.ALL:
            params["trial_metric_match"] = self.match.value
        return params

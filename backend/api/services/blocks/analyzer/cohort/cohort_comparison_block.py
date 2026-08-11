from __future__ import annotations

import json
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from oddish.blocks.block import Block

from api.services.blocks.analyzer.cohort import cohort_prompts as cp
from api.services.blocks.analyzer.cohort.cohort_taxonomy import BehaviorCategory
from api.services.blocks.analyzer.trajectory.trajectory_component_block import (
    TrajectoryBlockTaxonomy,
)

SCHEMA_VERSION = 1


def _non_empty_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    return value


NonEmptyText = Annotated[str, AfterValidator(_non_empty_text)]


class BehaviorEvidence(BaseModel):
    """One citation: a component that exists in a cohort trial's summary.

    Reusing TrajectoryBlockTaxonomy means the model never authors a link --
    trial_id plus step_ids is enough for the frontend to build the target.
    """

    model_config = ConfigDict(extra="forbid")

    trial_id: NonEmptyText
    trajectory_component: TrajectoryBlockTaxonomy
    step_ids: list[int]
    quote: NonEmptyText

    @model_validator(mode="after")
    def _steps_present(self) -> "BehaviorEvidence":
        if not self.step_ids:
            raise ValueError("step_ids must not be empty")
        return self


class BehaviorObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    behavior_description: NonEmptyText
    evidence: list[BehaviorEvidence]

    @model_validator(mode="after")
    def _evidence_present(self) -> "BehaviorObservation":
        if not self.evidence:
            raise ValueError("evidence must not be empty")
        return self


class CategoryComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: BehaviorCategory
    label: str | None = None
    successful: list[BehaviorObservation]
    failing: list[BehaviorObservation]

    @model_validator(mode="after")
    def _label_matches_category(self) -> "CategoryComparison":
        is_discovery = self.category is BehaviorCategory.BEHAVIOR_DISCOVERY
        if is_discovery and not (self.label or "").strip():
            raise ValueError("behavior_discovery requires a label")
        if not is_discovery and self.label is not None:
            raise ValueError("only behavior_discovery may carry a label")
        return self


class CohortComparisonOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    cohort_success: list[str]
    cohort_failure: list[str]
    categories: list[CategoryComparison]


class CohortInput(BaseModel):
    task_name: str
    successful: list[dict]
    failing: list[dict]


class _Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TaskIn(BaseModel):
    task_name: str


class _CohortIn(BaseModel):
    label: str
    trials: list[dict]


class CohortComparisonBlock(Block):
    output_schema = CohortComparisonOutput
    strict_json_output = True

    def __init__(
        self, cohort_input: CohortInput, *, instructions_template: str
    ) -> None:
        self.cohort_input = cohort_input
        self._instructions_template = instructions_template

    def sections(self) -> list[dict]:
        ci = self.cohort_input
        return [
            {
                "name": "preamble",
                "raw_input": {},
                "schema": _Empty,
                "formatter": lambda _d: cp.PREAMBLE,
            },
            {
                "name": "task",
                "raw_input": {"task_name": ci.task_name},
                "schema": _TaskIn,
                "formatter": lambda d: f"<task>\nName: {d.task_name}\n</task>",
            },
            {
                "name": "instructions",
                "raw_input": {},
                "schema": _Empty,
                "formatter": lambda _d: cp.instructions_section(
                    self._instructions_template
                ),
            },
            {
                "name": "successful",
                "raw_input": {"label": "SUCCESSFUL", "trials": ci.successful},
                "schema": _CohortIn,
                "formatter": lambda d: cp.cohort_section(d.label, d.trials),
            },
            {
                "name": "failing",
                "raw_input": {"label": "FAILING", "trials": ci.failing},
                "schema": _CohortIn,
                "formatter": lambda d: cp.cohort_section(d.label, d.trials),
            },
        ]

    def to_output(self, raw: str) -> dict:
        """Parse and validate. Raises ValueError on malformed output."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"model output was not JSON: {exc}") from exc
        parsed = CohortComparisonOutput(**data)
        # The block owns schema_version, not the model.
        parsed.schema_version = SCHEMA_VERSION
        return parsed.model_dump(mode="json")

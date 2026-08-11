from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

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

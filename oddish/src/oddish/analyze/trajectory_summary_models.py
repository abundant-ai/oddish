"""Structured-output contract for one trajectory-summary LLM request."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from oddish.analyze.trajectory_taxonomy import (
    ActionAxis,
    PurposeAxis,
    TrajectoryBlockTaxonomy,
)

NonEmptyText = Annotated[str, Field(min_length=1)]


class TrajectoryHighlightOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: int
    title: NonEmptyText
    why: NonEmptyText


class TrajectoryComponentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_ids: list[int] = Field(min_length=1)
    trajectory_component: TrajectoryBlockTaxonomy
    action: ActionAxis
    purpose: PurposeAxis
    summary: NonEmptyText


class TrajectorySummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: NonEmptyText
    highlights: list[TrajectoryHighlightOutput]
    components: list[TrajectoryComponentOutput] = Field(min_length=1)


class SummarizeResultOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_trial_id: NonEmptyText
    trajectory_summary: TrajectorySummaryOutput

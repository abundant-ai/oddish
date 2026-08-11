from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from oddish.blocks.block import Block

from api.services.blocks.analyzer.cohort import cohort_prompts as cp
from api.services.blocks.analyzer.cohort.cohort_taxonomy import BehaviorCategory

SCHEMA_VERSION = 1

# A trial whose summary covers less than this share of its own step span is
# reported to the reader rather than averaged over silently.
MIN_COVERAGE = 0.5


def _non_empty_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    return value


NonEmptyText = Annotated[str, AfterValidator(_non_empty_text)]


class BehaviorEvidence(BaseModel):
    """One citation: a component that exists in a cohort trial's summary.

    The model never authors a link -- trial_id plus step_ids is enough for the
    frontend to build the target.

    ``trajectory_component`` is deliberately a plain string, NOT the live
    ``TrajectoryBlockTaxonomy`` enum. Do not "tighten" it back: stored
    summaries still carry retired labels (``thinking_correction``,
    ``thinking_diagnose``, ``testing_custom_edge_cases``), the prompt shows
    those verbatim and tells the model to copy them exactly, so an enum here
    rejects citations that are perfectly correct. The enum was never the
    safety property anyway -- ``validate_evidence`` requires the label to match
    a component actually stored on that trial, which is strictly stronger.
    """

    model_config = ConfigDict(extra="forbid")

    trial_id: NonEmptyText
    trajectory_component: NonEmptyText
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
        # A stray label on a fixed category is dropped, not raised on. The
        # first real run put one on four of five categories, and because the
        # output is parsed as a whole, raising discarded an otherwise good
        # comparison over a cosmetic field. The prompt now says labels are
        # discovery-only; this keeps a model that ignores it from costing the
        # whole response.
        if not is_discovery:
            self.label = None
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
        """Parse, then drop every citation that does not resolve.

        Validation lives here, in the ``output_transform``, so the shape the
        AnalyzerBlock persists is already validated -- filtering after
        ``block.run()`` returns would validate only for the first caller and
        serve raw, unverified citations on every cache hit afterwards.

        Raises ``BlockParseError`` (a ValueError) on malformed output.
        """
        # Imported here: the service module is the caller of this block, and a
        # module-level import would make the pair mutually dependent.
        from api.services.cohort_comparison import validate_evidence

        parsed = self.parse(raw)
        # The block owns schema_version, not the model.
        parsed.schema_version = SCHEMA_VERSION
        ci = self.cohort_input
        out, dropped = validate_evidence(
            parsed.model_dump(mode="json"), ci.successful, ci.failing
        )
        out["dropped"] = dropped
        # Surfaced in the UI so a reader can see when the comparison rests on
        # thin evidence, rather than the feature averaging over it silently.
        out["thin_coverage"] = [
            t["trial_id"]
            for t in (ci.successful + ci.failing)
            if t.get("coverage", 0.0) < MIN_COVERAGE
        ]
        return out

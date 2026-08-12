from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from oddish.blocks.block import Block

from api.services.blocks.analyzer.cohort import cohort_prompts as cp
from api.services.blocks.analyzer.cohort.cohort_taxonomy import BehaviorCategory

# 2: added `summary`. 3: added `mode` and `models`, and relaxed the gate to one
# populated cohort -- stored rows carry neither field and were generated under
# the two-cohort framing, so they have to regenerate to gain either.
SCHEMA_VERSION = 3

# A trial whose summary covers less than this share of its own step span is
# reported to the reader rather than averaged over silently.
MIN_COVERAGE = 0.5


def _model_counts(trials: list[dict]) -> list[dict]:
    """[{model, trials}], most frequent first, then alphabetical for stability."""
    counts: dict[str, int] = {}
    for t in trials:
        name = cp.short_model_name(t.get("model") or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return [
        {"model": m, "trials": n}
        for m, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


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
    # LAST, and the order is load-bearing. This schema is handed to the model
    # as `response_format` / `output_schema`, and constrained decoding emits
    # fields in schema order -- so a `summary` declared above `categories`
    # would be generated before the rows it is supposed to be bound by,
    # exactly inverting the prompt's "write summary last" rule and inviting a
    # headline the categories do not support.
    summary: NonEmptyText


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
                "formatter": lambda _d: cp.preamble(
                    successful=ci.successful, failing=ci.failing
                ),
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
        # The headline was written against the categories the model produced,
        # and validation runs after it. If a whole category failed citation
        # checks and was removed, the summary can name a split the panel no
        # longer shows -- an unsourced claim sitting above sourced rows, which
        # is the one thing this feature is built not to do. Drop it rather
        # than let it describe a comparison that is no longer on screen.
        # Observation-level drops leave the category standing, so the theme
        # still holds and the summary survives them.
        if dropped.get("categories"):
            out.pop("summary", None)
        # Cohort membership is a fact we already hold, not something to take
        # from the model. The UI renders these lengths as "N successful, M
        # failing"; leaving the model's lists in place would let a fabricated
        # or truncated array misreport how much evidence the comparison rests
        # on -- the same class of problem the citation check exists to stop.
        out["cohort_success"] = [t["trial_id"] for t in ci.successful]
        out["cohort_failure"] = [t["trial_id"] for t in ci.failing]
        # Which models landed on which side, counted here rather than asked of
        # the model. "opus-4-8 succeeded 6 times" is arithmetic over rows we
        # already hold; routing it through an LLM would make a checkable fact
        # into an unverifiable claim, and validate_evidence has no way to
        # police a count.
        out["models"] = {
            "successful": _model_counts(ci.successful),
            "failing": _model_counts(ci.failing),
        }
        # trial -> model, so a citation can name the model it came from. The
        # chips say which models ran on a side; without this the reader cannot
        # tell which of them the cited behaviour belongs to, and a side listing
        # fourteen models next to one citation reads as if all fourteen did it.
        out["trial_models"] = {
            t["trial_id"]: cp.short_model_name(t.get("model") or "unknown")
            for t in (*ci.successful, *ci.failing)
        }
        # Single-cohort runs describe rather than compare, and the UI needs to
        # know which without re-deriving it from two list lengths.
        out["mode"] = (
            "comparison" if ci.successful and ci.failing else "single"
        )
        # Surfaced in the UI so a reader can see when the comparison rests on
        # thin evidence, rather than the feature averaging over it silently.
        out["thin_coverage"] = [
            t["trial_id"]
            for t in (ci.successful + ci.failing)
            if t.get("coverage", 0.0) < MIN_COVERAGE
        ]
        return out

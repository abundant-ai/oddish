from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


class Classification(str, Enum):
    """Top-level classification of a trial outcome."""

    HARNESS_ERROR = "HARNESS_ERROR"
    GOOD_FAILURE = "GOOD_FAILURE"
    BAD_FAILURE = "BAD_FAILURE"
    GOOD_SUCCESS = "GOOD_SUCCESS"
    BAD_SUCCESS = "BAD_SUCCESS"

    @property
    def is_task_problem(self) -> bool:
        return self in (Classification.BAD_FAILURE, Classification.BAD_SUCCESS)

    @property
    def is_success(self) -> bool:
        return self in (Classification.GOOD_SUCCESS, Classification.BAD_SUCCESS)


class TrialClassificationModel(BaseModel):
    """Pydantic model for trial-level structured output."""

    classification: Literal[
        "HARNESS_ERROR", "GOOD_FAILURE", "BAD_FAILURE", "GOOD_SUCCESS", "BAD_SUCCESS"
    ] = Field(description="Top-level classification")
    subtype: str = Field(
        description="Specific subtype from the taxonomy (e.g., 'Timeout', 'Underspecified Instruction')"
    )
    evidence: str = Field(
        description="Specific evidence from files: test names, error messages, code snippets"
    )
    root_cause: str = Field(
        description="1-2 sentence explanation of what caused this outcome"
    )
    recommendation: str = Field(
        description="How to fix the task (if the label marks a task problem), or 'N/A' if task is fine"
    )
    action_items: list[ActionItem] = Field(
        default_factory=list,
        description="New trajectory-derived action items (source=post_trial)",
    )
    exploitation: list[ExploitationAssessment] = Field(
        default_factory=list,
        description="Assessment of each provided pre-trial action item",
    )


class TaskVerdictModel(BaseModel):
    """Pydantic model for task-level structured output."""

    verdict: Literal["accept", "reject"] = Field(
        description="accept: the task works. reject: the task needs a fix."
    )
    confidence: Literal["high", "medium", "low"] = Field(description="Confidence level")
    primary_issue: str | None = Field(
        default=None, description="Primary issue if the task is rejected, else null"
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="Actionable recommendations (3-5 for rejected tasks)",
    )
    reasoning: str | None = Field(
        default=None, description="1-2 sentence explanation of the verdict (optional)"
    )

    @property
    def is_good(self) -> bool:
        return self.verdict == "accept"


@dataclass
class TrialClassification:
    """Classification result for a single trial."""

    trial_name: str
    classification: Classification
    subtype: str
    evidence: str
    root_cause: str
    recommendation: str
    reward: float | None = None
    action_items: list[ActionItem] = field(default_factory=list)
    exploitation: list[ExploitationAssessment] = field(default_factory=list)

    @property
    def is_task_problem(self) -> bool:
        # A HARNESS_ERROR hidden_file_leak voids the run, but the exposure
        # itself is a task defect (verdict rule: any leak -> is_good=false).
        if (
            self.classification is Classification.HARNESS_ERROR
            and self.subtype == "hidden_file_leak"
        ):
            return True
        return self.classification.is_task_problem

    @classmethod
    def from_model(
        cls,
        trial_name: str,
        model: TrialClassificationModel,
        reward: float | None = None,
    ) -> "TrialClassification":
        return cls(
            trial_name=trial_name,
            classification=Classification(model.classification),
            subtype=model.subtype,
            evidence=model.evidence,
            root_cause=model.root_cause,
            recommendation=model.recommendation,
            reward=reward,
            action_items=list(model.action_items),
            exploitation=list(model.exploitation),
        )


class ActionItemSource(str, Enum):
    PRE_TRIAL = "pre_trial"
    POST_TRIAL = "post_trial"


class ProblemType(str, Enum):
    INCOMPLETENESS = "incompleteness"
    MISMATCH = "mismatch"


class Dimension(str, Enum):
    VERIFIER = "verifier"
    ORACLE = "oracle"
    INFO_LEAKAGE = "info_leakage"


# Keyed by the heading text the prompt uses for each dimension. Only exact
# heading spellings are mapped: anything else stays as-is and fails validation,
# so a genuinely unknown dimension is still caught rather than coerced.
_DIMENSION_HEADING_SPELLINGS = {
    "verifier_completeness": Dimension.VERIFIER.value,
    "oracle_correctness": Dimension.ORACLE.value,
    "information_leakage": Dimension.INFO_LEAKAGE.value,
}


class ActionTier(str, Enum):
    MUST_FIX = "must_fix"
    SHOULD_FIX = "should_fix"
    OPTIONAL = "optional"


class ActionItem(BaseModel):
    """A single QA finding with a file/line anchor. Emitted by both the
    pre-trial and post-trial analyzers; the ``id`` is computed server-side
    (LLM output omits it)."""

    id: str | None = Field(
        default=None, description="Stable id; computed server-side, leave null"
    )
    source: ActionItemSource = Field(description="Which analyzer produced this item")
    problem_type: ProblemType = Field(description="incompleteness or mismatch")
    dimension: Dimension = Field(
        description="verifier, oracle, or info_leakage"
    )
    file: str = Field(description="Task-relative path, e.g. 'verifier.py'")
    line_start: int = Field(description="1-indexed first line")
    line_end: int = Field(description="1-indexed last line (== line_start if one line)")
    title: str = Field(description="Short one-line summary")
    detail: str = Field(description="What is wrong")
    recommendation: str = Field(description="Concrete fix")
    tier: ActionTier = Field(description="must_fix, should_fix, or optional")

    # post_trial-only linkage fields (defaults keep pre_trial items clean)
    links_to: str | None = Field(
        default=None, description="pre_trial ActionItem.id this relates to"
    )
    exploited: bool = Field(
        default=False, description="Did the trajectory exploit this weakness?"
    )
    exploit_evidence: str | None = Field(
        default=None, description="Quote or step reference showing exploitation"
    )
    causal: bool = Field(
        default=False, description="Did trajectory behavior result from this weakness?"
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_prompt_heading_spellings(cls, data: object) -> object:
        """Take the field names the prompt's own headings invite.

        The taxonomy is taught as prose sections -- "SEVERITY", "1. VERIFIER
        COMPLETENESS" -- and models fill the JSON from the heading rather than
        the field name: ``severity`` for ``tier``, ``verifier_completeness``
        for ``verifier``. Both name the right concept, and discarding an audit
        that cost minutes of agent time over the spelling is the worse error.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "tier" not in data and "severity" in data:
            data["tier"] = data.pop("severity")
        dimension = data.get("dimension")
        if isinstance(dimension, str):
            data["dimension"] = _DIMENSION_HEADING_SPELLINGS.get(
                dimension.strip().lower(), dimension
            )
        return data


class ExploitationAssessment(BaseModel):
    """Whether a pre-trial action item was exploited by this trial."""

    links_to: str = Field(description="Pre-trial ActionItem.id this assesses")
    exploited: bool = Field(description="Did the trajectory exploit this weakness?")
    exploit_evidence: str | None = Field(
        default=None, description="Quote or step index showing exploitation"
    )
    causal: bool = Field(
        default=False, description="Did trajectory behavior result from this weakness?"
    )


def compute_action_item_id(item: ActionItem) -> str:
    """Deterministic id from the item's identity fields (not its linkage state)."""
    raw = "|".join(
        [
            item.source.value,
            item.dimension.value,
            item.problem_type.value,
            item.file,
            str(item.line_start),
            str(item.line_end),
            item.title.strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


class PreTrialActionItems(BaseModel):
    """List wrapper so the block's output_schema is a dict-shaped model."""

    items: list[ActionItem] = Field(default_factory=list, description="Pre-trial QA findings")


# ---------------------------------------------------------------------------
# Agent-capabilities output schema.
#
# Moved verbatim from the retired backend cohort block
# (backend/api/services/blocks/analyzer/cohort/agent_capabilities_block.py and
# cohort_taxonomy.py) when the execution path was removed. Preserved here so
# the feature can return as a 'capabilities' analysis trial whose brief and
# importer build against the same schema, and so stored analyzer_blocks rows
# remain interpretable. Only the constant was renamed
# (SCHEMA_VERSION -> AGENT_CAPABILITIES_SCHEMA_VERSION) to fit a shared module.


# Discovery leads: it is the only category that can surface a behaviour the
# fixed vocabulary below does not anticipate. The remaining six deliberately
# mirror the task-rubric horizontals so the two artefacts share vocabulary.
class BehaviorCategory(str, Enum):
    BEHAVIOR_DISCOVERY = "behavior_discovery"
    PLANNING = "planning"
    TESTING_VERIFICATION = "testing_verification"
    DEBUGGING = "debugging"
    SCOPE_ADHERENCE = "scope_adherence"
    COHERENCE = "coherence"
    ENVIRONMENT_TOOLING = "environment_tooling"


CATEGORY_DEFINITIONS: dict[BehaviorCategory, str] = {
    BehaviorCategory.BEHAVIOR_DISCOVERY: (
        "Anything notable that none of the other categories covers. Use this "
        "for a pattern that distinguishes the cohorts but has no home below, "
        "and give it a short label naming the pattern."
    ),
    BehaviorCategory.PLANNING: (
        "Decomposition, staging and sequencing of the work, re-planning, and "
        "architecture or framework choices made before implementation."
    ),
    BehaviorCategory.TESTING_VERIFICATION: (
        "When and how tests or the verifier are run, whether a baseline was "
        "taken before editing, and what was checked before declaring the work "
        "done."
    ),
    BehaviorCategory.DEBUGGING: (
        "Diagnosis quality: forming a hypothesis, isolating a cause, and "
        "confirming it, as against cycling through speculative fixes."
    ),
    BehaviorCategory.SCOPE_ADHERENCE: (
        "Honouring the task's stated constraints, and whether code, tests or "
        "configuration were deleted, stubbed or disabled to force a green build."
    ),
    BehaviorCategory.COHERENCE: (
        "Holding one thread across a long run: whether context established "
        "early is lost and rediscovered, and whether settled decisions are "
        "re-litigated."
    ),
    BehaviorCategory.ENVIRONMENT_TOOLING: (
        "How the sandbox, available tools and offline resources were used, "
        "including time lost fighting them."
    ),
}

# behavior_discovery is the most valuable category and the only one without a
# fixed vocabulary constraining it, which makes it the likeliest to produce
# confident nonsense. Cap it.
DISCOVERY_CAP = 2

# 2: added `summary`. 3: added `mode` and `models`, and relaxed the gate to one
# populated cohort -- stored rows carry neither field and were generated under
# the two-cohort framing, so they have to regenerate to gain either. 4: the
# comparison reads the raw trajectories through CLAUDE_CLI, trials carry a
# counted `subagents` attribute, and evidence gained a step-level shape. Stored
# rows predate all three: they were compared from summaries alone, so their
# delegation findings are the old discovery-slot lottery result. 5 admits any
# number of trajectories, includes BAD_* and HARNESS_ERROR runs, and uses
# verifier outcomes provisionally when QA has not run.
AGENT_CAPABILITIES_SCHEMA_VERSION = 5


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
    safety property anyway -- the old ``validate_evidence`` required the label
    to match a component actually stored on that trial, which is strictly
    stronger.
    """

    model_config = ConfigDict(extra="forbid")

    trial_id: NonEmptyText
    quote: NonEmptyText
    # Summary-level: the quote must match this component's stored summary.
    trajectory_component: str | None = None
    step_ids: list[int] = Field(default_factory=list)
    # Step-level: the quote must appear in this raw step's own text. Only the
    # CLAUDE_CLI path can produce these, because only it can read the steps.
    step_id: int | None = None

    # A citation is checked against one thing, so it may name only one shape:
    # both at once leaves it ambiguous which source the quote must match, and
    # picking one would let a quote that matches neither survive by being
    # checked against the other. That rule was enforced by the old resolver,
    # which DROPPED the citation, and deliberately not by a validator that
    # raises. `model_json_schema` cannot express "exactly one of these", so a
    # schema handed to claude-code via `--json-schema` marks both optional and
    # constrained decoding can emit both or neither. A raise here would fail
    # `model_validate` for the whole payload and discard a minutes-long run
    # over one bad citation. Dropping costs one citation and keeps the
    # comparison.


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


class AgentCapabilitiesOutput(BaseModel):
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

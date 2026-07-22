from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


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


class Subtype(str, Enum):
    """Detailed subtype explaining the classification."""

    AGENT_NOT_FOUND = "Agent Not Found"
    CONTAINER_FAILURE = "Container/Docker Failure"
    MISSING_DEPENDENCIES = "Missing Dependencies"
    EMPTY_TRAJECTORY = "Empty Trajectory"
    INFRASTRUCTURE_ERROR = "Infrastructure Error"

    TIMEOUT = "Timeout"
    WRONG_APPROACH = "Wrong Approach"
    IMPLEMENTATION_BUGS = "Implementation Bugs"
    CONTEXT_LOSS = "Context Loss"
    PREMATURE_STOP = "Premature Stop"
    COMPLEXITY_OVERWHELM = "Complexity Overwhelm"
    INCOMPLETE_SOLUTION = "Incomplete Solution"
    LOGIC_ERROR = "Logic Error"

    UNDERSPECIFIED_INSTRUCTION = "Underspecified Instruction"
    RIGID_BRITTLE_TESTS = "Rigid/Brittle Tests"
    NONDETERMINISTIC_TESTS = "Non-deterministic Tests"
    ENVIRONMENT_ISSUES = "Environment Issues"
    MISSING_FILE_REFERENCE = "Missing File Reference"
    AMBIGUOUS_REQUIREMENTS = "Ambiguous Requirements"
    IMPLEMENTATION_DETAILS_REQUIRED = "Implementation Details Required"
    EDGE_CASES_NOT_SPECIFIED = "Edge Cases Not Specified"
    TEST_EXPECTS_SPECIFIC_FORMAT = "Test Expects Specific Format"

    CORRECT_SOLUTION = "Correct Solution"
    ALTERNATIVE_VALID_SOLUTION = "Alternative Valid Solution"

    HARDCODING = "Hardcoding"
    TEST_INSPECTION = "Test Inspection"
    ORACLE_COPYING = "Oracle Copying"
    MINIMAL_COMPLIANCE = "Minimal Compliance"
    TESTS_TOO_PERMISSIVE = "Tests Too Permissive"
    TASK_PRE_SOLVED = "Task Pre-solved"


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
        description="How to fix the task (if BAD_FAILURE or BAD_SUCCESS), or 'N/A' if task is fine"
    )


class TaskVerdictModel(BaseModel):
    """Pydantic model for task-level structured output."""

    is_good: bool = Field(
        description="Whether the task is good (true) or needs review (false)"
    )
    confidence: Literal["high", "medium", "low"] = Field(description="Confidence level")
    primary_issue: str | None = Field(
        default=None, description="Primary issue if task needs review, else null"
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="Actionable recommendations (3-5 for bad tasks)",
    )
    reasoning: str | None = Field(
        default=None, description="1-2 sentence explanation of the verdict (optional)"
    )


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

    @property
    def is_task_problem(self) -> bool:
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
        )


@dataclass
class BaselineResult:
    """Result from running a baseline agent (nop or oracle)."""

    agent: Literal["nop", "oracle"]
    passed: bool
    reward: float | None
    error: str | None = None

    @property
    def is_expected(self) -> bool:
        if self.agent == "nop":
            return not self.passed
        return self.passed


@dataclass
class BaselineValidation:
    """Results from baseline validation (nop and oracle runs)."""

    nop: BaselineResult | None = None
    oracle: BaselineResult | None = None

    @property
    def is_valid(self) -> bool:
        nop_ok = self.nop is None or self.nop.is_expected
        oracle_ok = self.oracle is None or self.oracle.is_expected
        return nop_ok and oracle_ok

    @property
    def issues(self) -> list[str]:
        issues = []
        if self.nop and not self.nop.is_expected:
            issues.append(
                "CRITICAL: nop agent passed - task may be pre-solved or tests are broken"
            )
        if self.oracle and not self.oracle.is_expected:
            issues.append(
                "CRITICAL: oracle agent failed - reference solution doesn't work"
            )
        return issues


@dataclass
class TaskVerdict:
    """Final verdict on task quality based on all analysis."""

    is_good: bool
    confidence: Literal["high", "medium", "low"]
    primary_issue: str | None
    reasoning: str | None = None
    recommendations: list[str] = field(default_factory=list)
    task_problem_count: int = 0
    agent_problem_count: int = 0
    success_count: int = 0
    harness_error_count: int = 0
    classifications: list[TrialClassification] = field(default_factory=list)
    baseline: BaselineValidation | None = None

    def summary(self) -> str:
        if self.is_good:
            return self.reasoning or f"GOOD TASK (confidence: {self.confidence})"
        return f"NEEDS REVIEW: {self.primary_issue}"


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

    items: list[ActionItem] = Field(default_factory=list)

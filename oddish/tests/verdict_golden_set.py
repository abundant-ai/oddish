"""Golden set of verdict-synthesis scenarios.

Each case pins one rule of ``verdict_prompt.txt`` to the verdict it must
produce. Two consumers:

* ``test_verdict_golden_set.py`` (offline, every CI run) -- each scenario's
  prompt renders and a synthetic judge reply round-trips the parse path.
  Guards prompt-rendering and schema regressions, says nothing about model
  behavior.
* ``test_verdict_golden_set_live.py`` (opt-in) -- the real verdict model
  judges every case, so a prompt rewrite that shifts the accept/reject line
  (as #1120 could have) is measured before it ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from oddish.analyze.models import (
    ActionItem,
    ActionItemSource,
    ActionTier,
    BaselineResult,
    BaselineValidation,
    Classification,
    Dimension,
    ProblemType,
    TrialClassification,
)
from oddish.blocks.analyzer.verdict.verdict_block import VerdictBlock


def _trial(name, classification, subtype, evidence, root_cause, reward):
    return TrialClassification(
        trial_name=name,
        classification=classification,
        subtype=subtype,
        evidence=evidence,
        root_cause=root_cause,
        recommendation="N/A",
        reward=reward,
    )


_VALID_BASELINE = BaselineValidation(
    nop=BaselineResult(agent="nop", passed=False, reward=0.0),
    oracle=BaselineResult(agent="oracle", passed=True, reward=1.0),
)

_WEAK_TESTS_TRIALS = [
    _trial(
        "t1",
        Classification.BAD_SUCCESS,
        "Tests Too Permissive",
        "The trial ran `verifier.py` against a stub that writes an empty "
        "`out.json`; it scored 0.9, above the 0.8 pass threshold.",
        "The verifier accepts work that does not solve the task.",
        1.0,
    ),
    _trial(
        "t2",
        Classification.GOOD_SUCCESS,
        "Correct Solution",
        "Implemented the parser in `report.py`; all 12 tests pass.",
        "The agent solved the task.",
        1.0,
    ),
    _trial(
        "t3",
        Classification.GOOD_FAILURE,
        "Implementation Bugs",
        "The agent's patch raises `KeyError` in `report.py:88`.",
        "The agent introduced its own bug.",
        0.0,
    ),
]

_VERIFIER_FINDING = ActionItem(
    source=ActionItemSource.PRE_TRIAL,
    problem_type=ProblemType.INCOMPLETENESS,
    dimension=Dimension.VERIFIER,
    file="verifier.py",
    line_start=12,
    line_end=18,
    title="verifier passes any non-empty `out.json`",
    detail="`verifier.py` only checks that `out.json` exists; it never "
    "compares contents against the expected fixture.",
    recommendation="Compare `out.json` to `expected/out.json` in `verifier.py`.",
    tier=ActionTier.MUST_FIX,
    exploited=True,
)


@dataclass(frozen=True)
class GoldenCase:
    name: str
    classifications: list[TrialClassification]
    expected_verdict: str
    expected_confidence: str
    primary_issue: str | None = None
    baseline: BaselineValidation | None = None
    quality_check_passed: bool = True
    pre_trial_items: list[ActionItem] | None = None
    # Scenario-defining substrings that must survive prompt rendering.
    expects_in_prompt: tuple[str, ...] = ()
    recommendations: list[str] = field(default_factory=list)
    reasoning: str = "See the trial analyses."

    def response(self) -> dict:
        """A well-formed judge reply for this case; the offline layer
        round-trips it through VerdictBlock's parse path."""
        return {
            "verdict": self.expected_verdict,
            "confidence": self.expected_confidence,
            "primary_issue": self.primary_issue,
            "recommendations": self.recommendations,
            "reasoning": self.reasoning,
        }


def build_block(case: GoldenCase) -> VerdictBlock:
    return VerdictBlock(
        case.classifications,
        baseline=case.baseline,
        quality_check_passed=case.quality_check_passed,
        pre_trial_items=case.pre_trial_items,
    )


GOLDEN_CASES: list[GoldenCase] = [
    GoldenCase(
        name="all_trials_good",
        classifications=[
            _trial(
                "t1",
                Classification.GOOD_SUCCESS,
                "Correct Solution",
                "Implemented retry logic in `client.py`; all 14 tests in "
                "`test_client.py` pass.",
                "The agent solved the task.",
                1.0,
            ),
            _trial(
                "t2",
                Classification.GOOD_FAILURE,
                "Implementation Bugs",
                "The agent's patch raises `KeyError` in `client.py:88`; "
                "`test_backoff` fails on the agent's own bug.",
                "The agent introduced a bug.",
                0.0,
            ),
            _trial(
                "t3",
                Classification.GOOD_FAILURE,
                "Incomplete Solution",
                "The agent ran out of steps before wiring `client.py` into "
                "`worker.py`; the task itself was clear.",
                "The agent did not finish.",
                0.0,
            ),
        ],
        baseline=_VALID_BASELINE,
        expected_verdict="accept",
        expected_confidence="high",
        reasoning="One trial solved the task fairly and the failures are "
        "agent mistakes.",
    ),
    GoldenCase(
        name="majority_same_failure",
        classifications=[
            _trial(
                f"t{i}",
                Classification.BAD_FAILURE,
                "Underspecified Instruction",
                "`test_retry.py` asserts the exact message 'E_TIMEOUT after "
                "3 attempts', which `instruction.md` never states and no "
                "file in the repo contains.",
                "The instruction omits a required output string.",
                0.0,
            )
            for i in (1, 2, 3)
        ]
        + [
            _trial(
                "t4",
                Classification.GOOD_FAILURE,
                "Implementation Bugs",
                "The agent's patch has a syntax error in `retry.py`.",
                "The agent introduced a bug.",
                0.0,
            )
        ],
        baseline=_VALID_BASELINE,
        expected_verdict="reject",
        expected_confidence="high",
        primary_issue="3/4 trials fail on an error string the instruction "
        "never states",
        recommendations=[
            "State the exact expected message in `instruction.md`",
            "Or match the message loosely in `test_retry.py`",
            "Re-run the trials after the fix",
        ],
        reasoning="Three of four trials hit the same unstated requirement.",
    ),
    GoldenCase(
        name="single_bad_label",
        classifications=[
            _trial(
                "t1",
                Classification.GOOD_SUCCESS,
                "Correct Solution",
                "All tests pass after a clean fix to `scheduler.py`.",
                "The agent solved the task.",
                1.0,
            ),
            _trial(
                "t2",
                Classification.GOOD_FAILURE,
                "Wrong Approach",
                "The agent rewrote the wrong module and never touched "
                "`scheduler.py`.",
                "The agent misread the task.",
                0.0,
            ),
            _trial(
                "t3",
                Classification.BAD_FAILURE,
                "Rigid/Brittle Tests",
                "The trial claims `test_output_format` is too strict about "
                "trailing whitespace.",
                "One trial's opinion; no other trial reports this.",
                0.0,
            ),
            _trial(
                "t4",
                Classification.GOOD_FAILURE,
                "Incomplete Solution",
                "The agent stopped after a partial fix.",
                "The agent did not finish.",
                0.0,
            ),
        ],
        baseline=_VALID_BASELINE,
        expected_verdict="accept",
        expected_confidence="low",
        reasoning="One fair success; the single BAD label is one trial's "
        "unconfirmed opinion.",
    ),
    GoldenCase(
        name="all_harness_errors",
        classifications=[
            _trial(
                "t1",
                Classification.HARNESS_ERROR,
                "Container/Docker Failure",
                "`docker build` failed: base image pull timed out.",
                "Infrastructure failure before the agent started.",
                None,
            ),
            _trial(
                "t2",
                Classification.HARNESS_ERROR,
                "Infrastructure Error",
                "The sandbox was evicted mid-run.",
                "Infrastructure failure.",
                None,
            ),
            _trial(
                "t3",
                Classification.HARNESS_ERROR,
                "Empty Trajectory",
                "The agent process produced no output at all.",
                "The harness never started the agent.",
                None,
            ),
        ],
        baseline=_VALID_BASELINE,
        expected_verdict="accept",
        expected_confidence="low",
        reasoning="Every trial is a harness error, so there is no evidence "
        "against the task.",
    ),
    GoldenCase(
        name="baseline_nop_passed",
        classifications=[
            _trial(
                "t1",
                Classification.BAD_SUCCESS,
                "Task Pre-solved",
                "`git diff` is empty yet `verifier.py` reports reward 1.0.",
                "The task is already solved in its initial state.",
                1.0,
            ),
            _trial(
                "t2",
                Classification.GOOD_SUCCESS,
                "Correct Solution",
                "The agent made a plausible edit and the tests pass.",
                "The agent solved the task (but so does doing nothing).",
                1.0,
            ),
        ],
        baseline=BaselineValidation(
            nop=BaselineResult(agent="nop", passed=True, reward=1.0),
            oracle=BaselineResult(agent="oracle", passed=True, reward=1.0),
        ),
        expected_verdict="reject",
        expected_confidence="high",
        primary_issue="CRITICAL: the nop baseline passed - the task is "
        "already solved in its initial state",
        expects_in_prompt=("CRITICAL: nop agent passed",),
        recommendations=[
            "Make the initial state fail `verifier.py`",
            "Add a test that the unmodified repo does not pass",
            "Re-run the nop baseline after the fix",
        ],
        reasoning="The nop baseline passed, so every pass is meaningless.",
    ),
    GoldenCase(
        name="weak_tests_single_trial",
        classifications=_WEAK_TESTS_TRIALS,
        baseline=_VALID_BASELINE,
        expected_verdict="reject",
        expected_confidence="medium",
        primary_issue="1/3 trials measured that the tests pass a stub " "solution",
        recommendations=[
            "Compare `out.json` contents in `verifier.py`, not just existence",
            "Add a negative test that a stub solution fails",
            "Re-run the trials after tightening the verifier",
        ],
        reasoning="A single trial measured weak tests and the static check "
        "has no matching finding.",
    ),
    GoldenCase(
        name="weak_tests_with_verifier_finding",
        classifications=_WEAK_TESTS_TRIALS,
        baseline=_VALID_BASELINE,
        pre_trial_items=[_VERIFIER_FINDING],
        expects_in_prompt=("[must_fix/verifier]", _VERIFIER_FINDING.title),
        expected_verdict="reject",
        expected_confidence="high",
        primary_issue="1/3 trials measured that the tests pass a stub "
        "solution, confirmed by a must_fix verifier finding",
        recommendations=[
            "Compare `out.json` contents in `verifier.py`, not just existence",
            "Add a negative test that a stub solution fails",
            "Re-run the trials after tightening the verifier",
        ],
        reasoning="The weak-tests measurement matches the audit's must_fix "
        "verifier finding.",
    ),
]

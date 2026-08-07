"""Deterministic verdict guardrails (oddish.analyze.verdict_rules).

The verdict prompt marks several rules as decisive on their own — a
``must_fix`` ``info_leakage`` finding, an access-hole subtype, a failed
baseline. The judge model has shipped "task is good" over inputs those rules
cover (motivating incident: a task whose static audit held a must_fix
info-leak finding and whose one BAD_SUCCESS trial measured the base model at
0.96 against a 0.25 pass threshold got is_good=true, low confidence). These
tests pin the code enforcement of those rules.
"""

from __future__ import annotations

import json

from oddish.analyze import enforce_verdict_guardrails
from oddish.analyze.models import (
    ActionItem,
    ActionItemSource,
    ActionTier,
    BaselineResult,
    BaselineValidation,
    Classification,
    Dimension,
    ProblemType,
    TaskVerdictModel,
    TrialClassification,
)
from oddish.blocks.analyzer.verdict.verdict_block import VerdictBlock


def _verdict(**over) -> TaskVerdictModel:
    base = dict(
        is_good=True,
        confidence="low",
        primary_issue=None,
        recommendations=[],
        reasoning="Five trials passed fairly.",
    )
    base.update(over)
    return TaskVerdictModel(**base)


def _classification(
    name: str = "t-1",
    classification: Classification = Classification.GOOD_SUCCESS,
    subtype: str = "legitimate_solution",
    **over,
) -> TrialClassification:
    base = dict(
        trial_name=name,
        classification=classification,
        subtype=subtype,
        evidence="evidence",
        root_cause="root cause",
        recommendation="N/A",
        reward=1.0,
    )
    base.update(over)
    return TrialClassification(**base)


def _item(
    tier: ActionTier = ActionTier.MUST_FIX,
    dimension: Dimension = Dimension.INFO_LEAKAGE,
    source: ActionItemSource = ActionItemSource.PRE_TRIAL,
    title: str = "The Dockerfile bakes the decryption passphrase into a RUN command.",
    recommendation: str = "Pass the passphrase as a BuildKit secret.",
) -> ActionItem:
    return ActionItem(
        source=source,
        problem_type=ProblemType.MISMATCH,
        dimension=dimension,
        file="environment/Dockerfile",
        line_start=122,
        line_end=129,
        title=title,
        detail="detail",
        recommendation=recommendation,
        tier=tier,
    )


# ---------------------------------------------------------------------------
# No rule fires -> verdict passes through untouched
# ---------------------------------------------------------------------------


def test_clean_inputs_leave_the_verdict_untouched():
    verdict = _verdict(confidence="high")
    out = enforce_verdict_guardrails(verdict, [_classification()])
    assert out is verdict


def test_should_fix_leak_and_optional_verifier_items_do_not_fire():
    out = enforce_verdict_guardrails(
        _verdict(),
        [_classification()],
        pre_trial_items=[
            _item(tier=ActionTier.SHOULD_FIX),
            _item(tier=ActionTier.OPTIONAL, dimension=Dimension.VERIFIER),
        ],
    )
    assert out.is_good is True


def test_isolated_bad_success_without_corroboration_stays_with_the_model():
    """One permissive_tests trial and no must_fix verifier audit finding is
    judgment territory — the model's call stands either way."""
    out = enforce_verdict_guardrails(
        _verdict(),
        [
            _classification(),
            _classification(
                "t-2", Classification.BAD_SUCCESS, subtype="permissive_tests"
            ),
        ],
    )
    assert out.is_good is True


# ---------------------------------------------------------------------------
# Rule: must_fix info_leakage finding -> is_good=false
# ---------------------------------------------------------------------------


def test_must_fix_leak_in_static_audit_forces_not_good():
    out = enforce_verdict_guardrails(
        _verdict(), [_classification()], pre_trial_items=[_item()]
    )
    assert out.is_good is False
    assert out.confidence == "high"
    assert "passphrase" in (out.primary_issue or "")
    assert "Pass the passphrase as a BuildKit secret." in out.recommendations
    assert "A fixed QA rule set is_good=false." in (out.reasoning or "")


def test_must_fix_leak_in_a_trial_action_item_forces_not_good():
    trial = _classification(
        action_items=[_item(source=ActionItemSource.POST_TRIAL)]
    )
    out = enforce_verdict_guardrails(_verdict(), [trial])
    assert out.is_good is False
    assert out.confidence == "high"


# ---------------------------------------------------------------------------
# Rule: access-hole subtypes -> is_good=false
# ---------------------------------------------------------------------------


def test_access_hole_subtype_forces_not_good():
    out = enforce_verdict_guardrails(
        _verdict(),
        [
            _classification(
                "t-1", Classification.BAD_SUCCESS, subtype="test_inspection"
            )
        ],
    )
    assert out.is_good is False
    assert out.confidence == "high"
    assert "t-1" in (out.primary_issue or "")


def test_access_hole_subtype_matches_legacy_enum_spelling():
    out = enforce_verdict_guardrails(
        _verdict(),
        [
            _classification(
                "t-1", Classification.BAD_SUCCESS, subtype="Oracle Copying"
            )
        ],
    )
    assert out.is_good is False


def test_harness_error_hidden_file_leak_forces_not_good():
    out = enforce_verdict_guardrails(
        _verdict(),
        [
            _classification(
                "t-1",
                Classification.HARNESS_ERROR,
                subtype="hidden_file_leak",
                reward=None,
            )
        ],
    )
    assert out.is_good is False


# ---------------------------------------------------------------------------
# Rule: failed baseline -> is_good=false, high confidence
# ---------------------------------------------------------------------------


def test_failed_baseline_forces_not_good_with_critical_issue():
    baseline = BaselineValidation(
        nop=BaselineResult(agent="nop", passed=True, reward=1.0)
    )
    out = enforce_verdict_guardrails(
        _verdict(), [_classification()], baseline=baseline
    )
    assert out.is_good is False
    assert out.confidence == "high"
    assert (out.primary_issue or "").startswith("CRITICAL:")


def test_valid_baseline_does_not_fire():
    baseline = BaselineValidation(
        nop=BaselineResult(agent="nop", passed=False, reward=0.0),
        oracle=BaselineResult(agent="oracle", passed=True, reward=1.0),
    )
    out = enforce_verdict_guardrails(
        _verdict(), [_classification()], baseline=baseline
    )
    assert out.is_good is True


# ---------------------------------------------------------------------------
# Rule: weak gate corroborated by the static audit -> is_good=false
# ---------------------------------------------------------------------------


def _weak_gate_inputs():
    classifications = [
        _classification(f"t-{i}") for i in range(1, 6)
    ] + [
        _classification(
            "t-6", Classification.BAD_SUCCESS, subtype="permissive_tests"
        )
    ]
    audit = [
        _item(
            dimension=Dimension.VERIFIER,
            title="The pass threshold is far below the constant-label baseline.",
            recommendation="Raise pass_threshold above the constant-label baseline.",
        )
    ]
    return classifications, audit


def test_bad_success_corroborated_by_must_fix_verifier_audit_forces_not_good():
    """The motivating incident, minus its leak item: five fair passes must not
    outvote a measured gate defect the static audit independently found."""
    classifications, audit = _weak_gate_inputs()
    out = enforce_verdict_guardrails(
        _verdict(), classifications, pre_trial_items=audit
    )
    assert out.is_good is False
    assert out.confidence == "medium"
    assert "t-6" in (out.primary_issue or "")
    assert (
        "Raise pass_threshold above the constant-label baseline."
        in out.recommendations
    )


def test_trial_side_verifier_item_does_not_corroborate_itself():
    """The corroborating must_fix verifier item must come from the static
    audit: a BAD_SUCCESS trial's own action item is the same source twice."""
    trial = _classification(
        "t-1",
        Classification.BAD_SUCCESS,
        subtype="permissive_tests",
        action_items=[
            _item(
                source=ActionItemSource.POST_TRIAL, dimension=Dimension.VERIFIER
            )
        ],
    )
    out = enforce_verdict_guardrails(_verdict(), [trial])
    assert out.is_good is True


# ---------------------------------------------------------------------------
# Field handling on override
# ---------------------------------------------------------------------------


def test_model_prose_is_kept_when_it_already_said_not_good():
    verdict = _verdict(
        is_good=False,
        confidence="low",
        primary_issue="The tests are too weak.",
        recommendations=["Fix the threshold."],
        reasoning="One trial showed weak gates.",
    )
    out = enforce_verdict_guardrails(
        verdict, [_classification()], pre_trial_items=[_item()]
    )
    assert out.is_good is False
    # Hard facts floor a not-good verdict's confidence upward...
    assert out.confidence == "high"
    # ...but the model's own prose stays first.
    assert out.primary_issue == "The tests are too weak."
    assert out.recommendations[0] == "Fix the threshold."
    assert out.reasoning is not None
    assert out.reasoning.startswith("One trial showed weak gates.")


def test_confidence_is_not_lowered_when_model_already_high():
    classifications, audit = _weak_gate_inputs()
    verdict = _verdict(
        is_good=False, confidence="high", primary_issue="Weak gate."
    )
    out = enforce_verdict_guardrails(
        verdict, classifications, pre_trial_items=audit
    )
    assert out.confidence == "high"


def test_recommendations_are_capped_at_five():
    verdict = _verdict(
        is_good=False,
        confidence="high",
        primary_issue="x",
        recommendations=[f"r{i}" for i in range(5)],
    )
    out = enforce_verdict_guardrails(
        verdict, [_classification()], pre_trial_items=[_item()]
    )
    assert len(out.recommendations) == 5
    assert out.recommendations == [f"r{i}" for i in range(5)]


def test_input_verdict_is_not_mutated():
    verdict = _verdict()
    enforce_verdict_guardrails(
        verdict, [_classification()], pre_trial_items=[_item()]
    )
    assert verdict.is_good is True
    assert verdict.recommendations == []


# ---------------------------------------------------------------------------
# End to end through VerdictBlock.to_verdict (the output_transform both the
# primary and the fallback synthesis arm run)
# ---------------------------------------------------------------------------


def test_to_verdict_applies_the_guardrails():
    vb = VerdictBlock(
        [_classification()],
        pre_trial_items=[_item()],
    )
    raw = json.dumps(
        {
            "is_good": True,
            "confidence": "low",
            "primary_issue": None,
            "recommendations": [],
            "reasoning": "Looks fine.",
        }
    )
    out = vb.to_verdict(raw)
    assert out["is_good"] is False
    assert out["confidence"] == "high"
    assert "A fixed QA rule set is_good=false." in out["reasoning"]


def test_to_verdict_leaves_clean_verdicts_alone():
    vb = VerdictBlock([_classification()])
    raw = json.dumps(
        {
            "is_good": True,
            "confidence": "high",
            "primary_issue": None,
            "recommendations": [],
            "reasoning": "fine",
        }
    )
    out = vb.to_verdict(raw)
    assert out["is_good"] is True
    assert out["reasoning"] == "fine"

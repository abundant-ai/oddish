"""Deterministic guardrails over the LLM-synthesized task verdict.

``verdict_prompt.txt`` states several rules that are not judgment calls: a
proven leak, an unintended-access subtype, or a failed baseline each force
``is_good=false`` on their own. The judge model does not reliably follow
them — a verdict has shipped "task is good" over a ``must_fix``
``info_leakage`` audit finding the prompt marks as decisive. Every rule that
is decidable from the verdict's own inputs is therefore re-applied here, in
code, on the parsed model output. The prompt keeps the rules so the model's
prose agrees with the enforced outcome; this module is what guarantees it.

Only rules whose trigger is a plain fact of the inputs belong here. Anything
that needs to weigh evidence (one trial's "underspecified instruction" claim,
a majority pattern) stays with the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from oddish.analyze.models import (
    ActionItem,
    ActionTier,
    BaselineValidation,
    Classification,
    Dimension,
    TaskVerdictModel,
    TrialClassification,
)

# Subtypes that prove unintended access by themselves (verdict prompt: "Any
# trial proves a leak or an access hole ... -> is_good=false, even when only
# one trial shows it"). Matched on the normalized spelling, since subtypes
# arrive both as prompt ids ("test_inspection") and legacy enum values
# ("Test Inspection").
_ACCESS_HOLE_SUBTYPES = frozenset(
    {
        "hidden_file_leak",
        "test_inspection",
        "oracle_copying",
        "unintended_access",
    }
)

# BAD_SUCCESS subtypes that say the pass gate accepted work that did not solve
# the task. One such trial alone stays with the model's judgment; combined
# with an *independent* must_fix verifier finding from the static audit it
# becomes decisive (see _weak_verifier_hit).
_WEAK_GATE_SUBTYPES = frozenset(
    {
        "permissive_tests",
        "tests_too_permissive",
        "task_pre_solved",
    }
)

_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
_MAX_RECOMMENDATIONS = 5


def _normalize_subtype(subtype: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (subtype or "").lower()).strip("_")


@dataclass
class _RuleHit:
    reason: str
    confidence: str
    recommendation: str | None = None


def _iter_action_items(
    classifications: list[TrialClassification],
    pre_trial_items: list[ActionItem] | None,
) -> list[ActionItem]:
    items = list(pre_trial_items or [])
    for classification in classifications:
        items.extend(classification.action_items)
    return items


def _baseline_hit(baseline: BaselineValidation | None) -> _RuleHit | None:
    if baseline is None or baseline.is_valid:
        return None
    return _RuleHit(
        reason="CRITICAL: " + " ".join(baseline.issues),
        confidence="high",
    )


def _leak_item_hit(
    classifications: list[TrialClassification],
    pre_trial_items: list[ActionItem] | None,
) -> _RuleHit | None:
    for item in _iter_action_items(classifications, pre_trial_items):
        if item.tier is ActionTier.MUST_FIX and item.dimension is Dimension.INFO_LEAKAGE:
            return _RuleHit(
                reason=(
                    "A `must_fix` information-leak finding proves the task "
                    f"exposes data it must hide (`{item.file}`): {item.title}"
                ),
                confidence="high",
                recommendation=item.recommendation,
            )
    return None


def _access_subtype_hit(
    classifications: list[TrialClassification],
) -> _RuleHit | None:
    for classification in classifications:
        if _normalize_subtype(classification.subtype) in _ACCESS_HOLE_SUBTYPES:
            return _RuleHit(
                reason=(
                    f"Trial `{classification.trial_name}` proves an access "
                    f"hole in the task (`{classification.subtype}`)."
                ),
                confidence="high",
                recommendation=classification.recommendation or None,
            )
    return None


def _weak_verifier_hit(
    classifications: list[TrialClassification],
    pre_trial_items: list[ActionItem] | None,
) -> _RuleHit | None:
    """Static audit and a trial independently found the same broken gate.

    The item side deliberately reads only the pre-trial audit, never trial
    action items: a BAD_SUCCESS trial usually files its own verifier item, and
    counting that would let one trial corroborate itself.
    """
    weak_trial = next(
        (
            c
            for c in classifications
            if c.classification is Classification.BAD_SUCCESS
            and _normalize_subtype(c.subtype) in _WEAK_GATE_SUBTYPES
        ),
        None,
    )
    if weak_trial is None:
        return None
    for item in pre_trial_items or []:
        if item.tier is ActionTier.MUST_FIX and item.dimension is Dimension.VERIFIER:
            return _RuleHit(
                reason=(
                    f"Trial `{weak_trial.trial_name}` passed without solving "
                    f"the task (`{weak_trial.subtype}`), and the static audit "
                    f"independently found a `must_fix` verifier defect "
                    f"(`{item.file}`): {item.title}"
                ),
                confidence="medium",
                recommendation=item.recommendation,
            )
    return None


def enforce_verdict_guardrails(
    verdict: TaskVerdictModel,
    classifications: list[TrialClassification],
    *,
    baseline: BaselineValidation | None = None,
    pre_trial_items: list[ActionItem] | None = None,
) -> TaskVerdictModel:
    """Apply the prompt's deterministic rules to a parsed verdict.

    Returns the verdict unchanged when no rule fires. When one does, the
    verdict is forced to ``is_good=false``; confidence is floored at the
    rule's own level, and the model's confidence in a wrong "good" call is
    discarded rather than floored. ``primary_issue``, ``recommendations``,
    and ``reasoning`` keep the model's prose where it exists and are filled
    from the firing rules where it does not, with the override stated in
    ``reasoning`` so the reader sees why the fields can disagree with the
    model's own trial-weighing.
    """
    hits = [
        hit
        for hit in (
            _baseline_hit(baseline),
            _leak_item_hit(classifications, pre_trial_items),
            _access_subtype_hit(classifications),
            _weak_verifier_hit(classifications, pre_trial_items),
        )
        if hit is not None
    ]
    if not hits:
        return verdict

    forced = verdict.model_copy(deep=True)
    floor = max(_CONFIDENCE_ORDER[hit.confidence] for hit in hits)
    if forced.is_good:
        confidence_rank = floor
    else:
        confidence_rank = max(floor, _CONFIDENCE_ORDER[forced.confidence])
    for name, rank in _CONFIDENCE_ORDER.items():
        if rank == confidence_rank:
            forced.confidence = name  # type: ignore[assignment]

    forced.is_good = False
    if not forced.primary_issue:
        forced.primary_issue = hits[0].reason

    recommendations = list(forced.recommendations)
    for hit in hits:
        if hit.recommendation and hit.recommendation not in recommendations:
            recommendations.append(hit.recommendation)
    forced.recommendations = recommendations[:_MAX_RECOMMENDATIONS]

    override_note = "A fixed QA rule set is_good=false. " + " ".join(
        hit.reason for hit in hits
    )
    forced.reasoning = (
        f"{forced.reasoning.rstrip()} {override_note}"
        if forced.reasoning
        else override_note
    )
    return forced

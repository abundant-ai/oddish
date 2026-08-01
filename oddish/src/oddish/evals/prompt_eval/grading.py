"""Grade a prompt run's output against a golden case's expected labels.

Pure functions: the runner supplies the model output as plain dicts so every
grader is unit-testable without an LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GradeResult:
    passed: bool
    detail: str


def grade(kind: str, expected: dict, output: dict) -> GradeResult:
    grader = _GRADERS.get(kind)
    if grader is None:
        raise ValueError(f"no grader for prompt kind {kind!r}")
    return grader(expected, output)


def _grade_post_trial(expected: dict, output: dict) -> GradeResult:
    """Expected keys: ``classification`` or ``classification_in`` (required),
    ``subtype_in`` (optional)."""
    predicted = str(output.get("classification") or "")
    allowed = expected.get("classification_in") or [expected.get("classification")]
    allowed = [value for value in allowed if value]
    if not allowed:
        raise ValueError("expected needs 'classification' or 'classification_in'")
    if predicted not in allowed:
        return GradeResult(
            passed=False,
            detail=f"classification {predicted or '(none)'} not in {allowed}",
        )
    subtype_in = expected.get("subtype_in")
    if subtype_in:
        predicted_subtype = str(output.get("subtype") or "")
        if predicted_subtype not in subtype_in:
            return GradeResult(
                passed=False,
                detail=f"subtype {predicted_subtype or '(none)'} not in {subtype_in}",
            )
    return GradeResult(passed=True, detail=f"classification {predicted}")


def _item_matches(matcher: dict, item: dict) -> bool:
    if "dimension" in matcher and item.get("dimension") != matcher["dimension"]:
        return False
    if "tier_in" in matcher and item.get("tier") not in matcher["tier_in"]:
        return False
    if "file_contains" in matcher:
        if matcher["file_contains"].lower() not in str(item.get("file") or "").lower():
            return False
    return True


def _describe_matcher(matcher: dict) -> str:
    parts = [
        f"{key}={matcher[key]}"
        for key in ("dimension", "tier_in", "file_contains")
        if key in matcher
    ]
    return ", ".join(parts) or "(any item)"


def _grade_pre_trial(expected: dict, output: dict) -> GradeResult:
    """Expected keys: ``items`` — matchers that must each hit at least one
    finding — and optional ``forbidden`` matchers that must hit none. Matcher
    fields: ``dimension``, ``tier_in``, ``file_contains``."""
    items = output.get("items") or []
    problems: list[str] = []
    for matcher in expected.get("items") or []:
        if not any(_item_matches(matcher, item) for item in items):
            problems.append(f"no finding matched [{_describe_matcher(matcher)}]")
    for matcher in expected.get("forbidden") or []:
        hits = [item for item in items if _item_matches(matcher, item)]
        if hits:
            title = str(hits[0].get("title") or "")[:80]
            problems.append(
                f"forbidden matcher [{_describe_matcher(matcher)}] hit "
                f"{len(hits)} finding(s), e.g. {title!r}"
            )
    if problems:
        return GradeResult(passed=False, detail="; ".join(problems))
    return GradeResult(
        passed=True, detail=f"{len(items)} finding(s), all expectations met"
    )


def _grade_verdict(expected: dict, output: dict) -> GradeResult:
    """Expected keys: ``is_good`` (required), ``confidence_in`` (optional)."""
    if "is_good" not in expected:
        raise ValueError("expected needs 'is_good'")
    predicted = output.get("is_good")
    if bool(predicted) != bool(expected["is_good"]):
        return GradeResult(
            passed=False,
            detail=f"is_good={predicted} wanted {expected['is_good']}",
        )
    confidence_in = expected.get("confidence_in")
    if confidence_in:
        confidence = str(output.get("confidence") or "")
        if confidence not in confidence_in:
            return GradeResult(
                passed=False,
                detail=f"confidence {confidence or '(none)'} not in {confidence_in}",
            )
    return GradeResult(passed=True, detail=f"is_good={predicted}")


_GRADERS = {
    "QA_POST_TRIAL": _grade_post_trial,
    "QA_PRE_TRIAL": _grade_pre_trial,
    "VERDICT": _grade_verdict,
}

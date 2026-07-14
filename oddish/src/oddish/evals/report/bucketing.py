"""Deterministic (no-LLM) roster/bucketing over SubAnalysis.

Buckets map directly onto the Classification enum; subcategories seed from the
Subtype enum. Any good-bucket subtype not in the seed map counts as 'emergent'
(the reduce step may name emergent categories; this rollup only counts them).
"""

from __future__ import annotations

from oddish.evals.primitives import SubAnalysis

BUCKET_OF: dict[str, str] = {
    "BAD_SUCCESS": "bad",
    "BAD_FAILURE": "bad",
    "GOOD_FAILURE": "good",
    "GOOD_SUCCESS": "other",
    "HARNESS_ERROR": "other",
}

# Seed subcategory rubric (Subtype value -> code). Bad: 1a spec / 1b construction.
# Good: 3a problem-id / 3b implementation / 3c syntax.
SUBCATEGORY_OF: dict[str, str] = {
    # 1a task ambiguity / specification
    "Underspecified Instruction": "1a",
    "Ambiguous Requirements": "1a",
    "Implementation Details Required": "1a",
    "Edge Cases Not Specified": "1a",
    "Missing File Reference": "1a",
    # 1b task security / construction
    "Rigid/Brittle Tests": "1b",
    "Tests Too Permissive": "1b",
    "Test Inspection": "1b",
    "Oracle Copying": "1b",
    "Hardcoding": "1b",
    "Task Pre-solved": "1b",
    "Non-deterministic Tests": "1b",
    "Test Expects Specific Format": "1b",
    "Minimal Compliance": "1b",
    # 3a problem identification
    "Wrong Approach": "3a",
    "Context Loss": "3a",
    "Complexity Overwhelm": "3a",
    # 3b implementation (method largely correct)
    "Implementation Bugs": "3b",
    "Logic Error": "3b",
    "Incomplete Solution": "3b",
    "Premature Stop": "3b",
    # 3c syntax — no canonical Subtype today; populated by emergent/LLM labels.
}


def bucket_subanalyses(
    subanalyses: list[SubAnalysis],
) -> tuple[list[SubAnalysis], list[SubAnalysis], dict[str, int]]:
    bad: list[SubAnalysis] = []
    good: list[SubAnalysis] = []
    breakdown: dict[str, int] = {}
    for sa in subanalyses:
        bucket = BUCKET_OF.get(sa.classification, "other")
        if bucket == "bad":
            bad.append(sa)
        elif bucket == "good":
            good.append(sa)
        else:
            continue
        code = SUBCATEGORY_OF.get(sa.subtype, "emergent")
        breakdown[code] = breakdown.get(code, 0) + 1
    return bad, good, breakdown

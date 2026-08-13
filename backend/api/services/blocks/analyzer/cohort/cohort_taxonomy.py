from __future__ import annotations

import enum

# Discovery leads: it is the only category that can surface a behaviour the
# fixed vocabulary below does not anticipate. The remaining six deliberately
# mirror the task-rubric horizontals so the two artefacts share vocabulary.
class BehaviorCategory(str, enum.Enum):
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

"""Pure metric functions for the cohort behavior charts.

No I/O: every input is a plain dict lifted out of a stored comparison, so
these are unit-testable without a database or a model call.
"""
from __future__ import annotations

# behavior_discovery is deliberately absent. DISCOVERY_CAP bounds its evidence
# at two observations, so its magnitude is short regardless of how the agents
# behaved -- plotting it beside six uncapped categories makes a schema artifact
# look like a finding.
CHART_CATEGORIES: tuple[str, ...] = (
    "planning",
    "testing_verification",
    "debugging",
    "scope_adherence",
    "coherence",
    "environment_tooling",
)

# Distinct cited trials below which a cell is drawn faded rather than solid.
THIN_N = 5

# Overlapping translucent radar shapes stop being readable past three, and the
# all-pairs colour-separation rule caps categorical series at three as well.
RADAR_MODEL_CAP = 3


def cited_trials(category: dict, side: str) -> set[str]:
    """Distinct trial ids cited on one side of one category.

    A set, not a count: one observation may cite the same trial several times,
    and counting citations would let a chatty observation outweigh a pattern
    that held across separate runs.
    """
    out: set[str] = set()
    for obs in category.get(side) or []:
        for ev in (obs or {}).get("evidence") or []:
            trial_id = (ev or {}).get("trial_id")
            if trial_id:
                out.add(trial_id)
    return out


def delta(
    cited_success: int,
    cited_failure: int,
    baseline: float,
    *,
    comparable: bool = True,
) -> float | None:
    """How far this category's citations beat the model's own success share.

    A trial's cohort side is fixed by its classification, so the raw ratio is
    dominated by how often the model succeeded at all and every category ends
    up redrawing the win rate. Subtracting the model's own baseline leaves the
    behavioural signal.

    None when nothing was cited: no evidence is not the same claim as no
    advantage, and a zero would plot as the latter.

    None too when ``comparable`` is False, meaning the cohort behind the
    baseline is one-sided. Every citable trial then sits on the side the
    baseline is pinned to, so the ratio equals it exactly and every category
    returns 0.0 -- which the radar draws as a closed shape resting on the
    baseline ring, the picture of a model that is average at everything. A
    model that never succeeded once has no behavioural signal at all, and
    saying so is the opposite of what that shape says.
    """
    n = cited_success + cited_failure
    if n == 0 or not comparable:
        return None
    return cited_success / n - baseline

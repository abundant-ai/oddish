"""Prompt text for CohortComparisonBlock.

Kept apart from the block logic so prompt edits do not touch parsing.
"""
from __future__ import annotations

from pathlib import Path

from oddish import analyze as _analyze

from api.services.blocks.analyzer.cohort.cohort_taxonomy import (
    CATEGORY_DEFINITIONS,
    DISCOVERY_CAP,
    BehaviorCategory,
)

PREAMBLE = (
    "You are comparing two cohorts of recorded agent runs on the same task: "
    "runs that succeeded for good reasons, and runs that failed for good "
    "reasons. A developer wants to know what the successful runs did "
    "differently."
)


def taxonomy_section() -> str:
    """The categories WITH definitions.

    The trajectory-summary prompt ships its labels as one bare comma-separated
    line and mislabels systematically as a result; do not repeat that here.
    """
    lines = ["<categories>"]
    for member in BehaviorCategory:
        lines.append(f"- {member.value}: {CATEGORY_DEFINITIONS[member]}")
    lines.append("</categories>")
    lines.append(
        f"Report at most {DISCOVERY_CAP} observations per side for "
        f"{BehaviorCategory.BEHAVIOR_DISCOVERY.value}."
    )
    return "\n".join(lines)


def cohort_section(label: str, trials: list[dict]) -> str:
    """One cohort's trials, as component streams the model can cite."""
    lines = [f"<cohort name=\"{label}\">"]
    for t in trials:
        lines.append(f'  <trial id="{t["trial_id"]}">')
        for c in t.get("components") or []:
            ids = c.get("step_ids") or []
            if not ids:
                continue
            rng = f"[{min(ids)}-{max(ids)}]"
            lines.append(
                f'    {c.get("trajectory_component")} {rng} '
                f'{(c.get("summary") or "").strip()}'
            )
        lines.append("  </trial>")
    lines.append("</cohort>")
    return "\n".join(lines)


def instructions_section(template: str) -> str:
    # str.replace, not .format: the template body contains JSON braces.
    return template.replace("{{taxonomy}}", taxonomy_section())


# Resolve via the oddish package's own location, matching
# summarize_trajectory.py:43. Do NOT walk parents[] from this file: the count
# is wrong (parents[5] is `backend/`, not the repo root) and it breaks the
# moment the module moves.
_PROMPT_PATH = (
    Path(_analyze.__file__).resolve().parent / "prompts" / "cohort_comparison.txt"
)


def load_cohort_prompt_template() -> str:
    return _PROMPT_PATH.read_text()

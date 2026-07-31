"""Prompt text for the verdict-synthesis VerdictBlock.

The prompt body itself is owned by ``oddish.analyze.build_verdict_prompt``, not
re-authored here. This module only adapts that call into the Block section
contract.
"""

from __future__ import annotations

from oddish.analyze import build_verdict_prompt
from oddish.analyze.models import (
    ActionItem,
    BaselineValidation,
    TrialClassification,
)


def verdict_section(
    classifications: list[TrialClassification],
    baseline: BaselineValidation | None,
    quality_check_passed: bool,
    pre_trial_items: list[ActionItem] | None = None,
) -> str:
    return build_verdict_prompt(
        classifications, baseline, quality_check_passed, pre_trial_items
    )

"""Prompt text for the verdict-synthesis VerdictBlock.

The prompt body itself is owned by ``oddish.analyze.build_verdict_prompt``, not
re-authored here -- reusing it is what makes the block path and the legacy
path parity-testable. This module only adapts that call into the Block
section contract.
"""

from __future__ import annotations

from oddish.analyze import build_verdict_prompt
from oddish.analyze.models import BaselineValidation, TrialClassification


def verdict_section(
    classifications: list[TrialClassification],
    baseline: BaselineValidation | None,
    quality_check_passed: bool,
) -> str:
    return build_verdict_prompt(classifications, baseline, quality_check_passed)

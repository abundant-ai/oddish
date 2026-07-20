from __future__ import annotations

from oddish.preflight.checks import (
    anti_cheat_soundness,
    closed_internet,
    provenance,
    solution_format,
)
from oddish.preflight.models import Check

# Populated as each check lands. Order is display order.
CHECKS: list[Check] = [
    Check(
        id=closed_internet.CHECK_ID,
        description="Open internet requires a justification",
        fn=closed_internet.check,
    ),
    Check(
        id=provenance.CHECK_ID,
        description="No repo fetches or .git in the agent image",
        fn=provenance.check,
    ),
    Check(
        id=solution_format.CHECK_ID,
        description="Solutions are readable source, not patches",
        fn=solution_format.check,
    ),
    Check(
        id=anti_cheat_soundness.CHECK_ID,
        description="No brittle source-scanning anti-cheat",
        fn=anti_cheat_soundness.check,
    ),
]

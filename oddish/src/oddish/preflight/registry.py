from __future__ import annotations

from oddish.preflight.checks import closed_internet, solution_format
from oddish.preflight.models import Check

# Populated as each check lands. Order is display order.
CHECKS: list[Check] = [
    Check(
        id=closed_internet.CHECK_ID,
        description="Open internet requires a justification",
        fn=closed_internet.check,
    ),
    Check(
        id=solution_format.CHECK_ID,
        description="Solutions are readable source, not patches",
        fn=solution_format.check,
    ),
]

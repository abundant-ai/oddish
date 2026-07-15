from __future__ import annotations

from oddish.preflight.checks import closed_internet
from oddish.preflight.models import Check

# Populated as each check lands. Order is display order.
CHECKS: list[Check] = [
    Check(
        id=closed_internet.CHECK_ID,
        description="Open internet requires a justification",
        fn=closed_internet.check,
    ),
]

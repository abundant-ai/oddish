from __future__ import annotations

from oddish.preflight.checks import dockerfile_leaks
from oddish.preflight.models import Check

CHECKS: list[Check] = [
    Check(
        id=dockerfile_leaks.CHECK_ID,
        description="Solution and tests stay out of the agent image",
        fn=dockerfile_leaks.check,
    ),
]

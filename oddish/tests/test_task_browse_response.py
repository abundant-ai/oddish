from __future__ import annotations

from pathlib import Path
import math
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints import (  # noqa: E402
    _finite_float_or_none,
    _finite_float_or_zero,
)


def test_task_browse_float_sanitizers_reject_non_finite_values():
    assert _finite_float_or_none(None) is None
    assert _finite_float_or_none(float("nan")) is None
    assert _finite_float_or_none(float("inf")) is None
    assert _finite_float_or_none("-inf") is None
    assert _finite_float_or_none("0.75") == 0.75

    assert _finite_float_or_zero(float("nan")) == 0.0
    assert _finite_float_or_zero(math.inf) == 0.0
    assert _finite_float_or_zero("1.25") == 1.25

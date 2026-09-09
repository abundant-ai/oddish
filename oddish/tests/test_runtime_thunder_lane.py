from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.runtime.sandbox_lifecycle import (
    DEFAULT_EXECUTION_LANE,
    EC2_TRIAL_EXECUTION_LANE,
    THUNDER_TRIAL_EXECUTION_LANE,
    execution_lane_for_environment,
)


def test_thunder_uses_dedicated_execution_lane() -> None:
    assert execution_lane_for_environment("thunder") == THUNDER_TRIAL_EXECUTION_LANE
    assert execution_lane_for_environment(" THUNDER ") == THUNDER_TRIAL_EXECUTION_LANE


def test_existing_execution_lanes_are_preserved() -> None:
    assert execution_lane_for_environment("ec2") == EC2_TRIAL_EXECUTION_LANE
    assert execution_lane_for_environment("modal") == DEFAULT_EXECUTION_LANE
    assert execution_lane_for_environment(None) == DEFAULT_EXECUTION_LANE

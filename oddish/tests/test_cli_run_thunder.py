from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType

run_module = importlib.import_module("oddish.cli.run")


def test_thunder_is_hosted_passthrough_when_harbor_exposes_it() -> None:
    assert EnvironmentType.THUNDER in run_module._HOSTED_PASSTHROUGH_ENVIRONMENTS

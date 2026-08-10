from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType  # noqa: E402
from oddish.cli import app  # noqa: E402


run_module = importlib.import_module("oddish.cli.run")


def test_ec2_is_a_hosted_passthrough_environment() -> None:
    assert EnvironmentType.EC2 in run_module._HOSTED_PASSTHROUGH_ENVIRONMENTS


def test_run_help_lists_ec2_and_keeps_daytona_as_the_hosted_cpu_default() -> None:
    result = CliRunner().invoke(app, ["run", "--help"], env={"COLUMNS": "240"})

    assert result.exit_code == 0
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    output = re.sub(r"\s+", " ", output)
    assert "ec2, e2b, modal" in output
    assert "Defaults: daytona" in output
    assert "for CPU-only" in output

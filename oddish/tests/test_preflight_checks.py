from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Severity
from oddish.preflight.checks import closed_internet


def _config(task_dir: Path) -> TaskConfig:
    return TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())


_OPEN_TOML = """\
version = "1.0"

[metadata]
description = "open task"

[environment]
network_mode = "public"
"""

_IMPLICIT_TOML = """\
version = "1.0"

[metadata]
description = "no network config at all"
"""

_CLOSED_TOML = """\
version = "1.0"

[metadata]
description = "closed task"

[environment]
network_mode = "no-network"
"""

_JUSTIFIED_TOML = """\
version = "1.0"

[metadata]
description = "open but justified"
open_internet_justification = "Needs live PyPI to resolve the dependency graph under test."

[environment]
network_mode = "public"
"""


def test_closed_internet_flags_explicit_public(make_task):
    task_dir = make_task(task_toml=_OPEN_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "environment baseline" in findings[0].message


def test_closed_internet_flags_implicit_public_default(make_task):
    # Harbor defaults network_mode to public, so a task.toml that says nothing
    # about the network is fully open. This is the case that matters most.
    task_dir = make_task(task_toml=_IMPLICIT_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_closed_internet_passes_no_network(make_task):
    task_dir = make_task(task_toml=_CLOSED_TOML)
    assert closed_internet.check(task_dir, _config(task_dir)) == []


def test_closed_internet_passes_with_a_real_justification(make_task):
    task_dir = make_task(task_toml=_JUSTIFIED_TOML)
    assert closed_internet.check(task_dir, _config(task_dir)) == []


@pytest.mark.parametrize("placeholder", ["TBD", "todo", "n/a", "xxx", "  ", "short"])
def test_closed_internet_rejects_placeholder_justifications(make_task, placeholder):
    toml = _OPEN_TOML.replace(
        'description = "open task"',
        f'description = "open task"\nopen_internet_justification = "{placeholder}"',
    )
    task_dir = make_task(task_toml=toml)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_closed_internet_flags_an_open_agent_over_a_closed_baseline(make_task):
    toml = _CLOSED_TOML + '\n[agent]\nnetwork_mode = "public"\n'
    task_dir = make_task(task_toml=toml)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert "[agent]" in findings[0].message


def test_closed_internet_passes_allowlist(make_task):
    toml = """\
version = "1.0"

[metadata]
description = "allowlisted"

[environment]
network_mode = "allowlist"
allowed_hosts = ["pypi.org"]
"""
    task_dir = make_task(task_toml=toml)
    assert closed_internet.check(task_dir, _config(task_dir)) == []

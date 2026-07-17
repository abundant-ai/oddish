from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Severity


def _config(task_dir: Path) -> TaskConfig:
    return TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())


from oddish.preflight.checks import closed_internet

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

# A separate verifier container that is public, over an otherwise fully closed
# task. The hand-rolled draft missed this entirely.
_VERIFIER_ENV_OPEN_TOML = """\
version = "1.0"

[metadata]
description = "closed everywhere except a separate verifier container"

[environment]
network_mode = "no-network"

[agent]
network_mode = "no-network"

[verifier]
network_mode = "no-network"

[verifier.environment]
docker_image = "ubuntu:22.04"
"""

# A per-step agent override that reopens the network on an otherwise closed task.
_STEP_OPEN_TOML = """\
version = "1.0"

[metadata]
description = "closed task with one step that reopens the agent network"

[environment]
network_mode = "no-network"

[agent]
network_mode = "no-network"

[verifier]
network_mode = "no-network"

[[steps]]
name = "s1"

[steps.agent]
network_mode = "public"
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
    assert "[environment]" in findings[0].message


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


def test_closed_internet_flags_a_public_separate_verifier_container(make_task):
    task_dir = make_task(task_toml=_VERIFIER_ENV_OPEN_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "verifier.environment" in findings[0].message


def test_closed_internet_flags_a_per_step_agent_override(make_task):
    task_dir = make_task(task_toml=_STEP_OPEN_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "s1" in findings[0].message


def test_closed_internet_passes_with_a_real_justification(make_task):
    task_dir = make_task(task_toml=_JUSTIFIED_TOML)
    assert closed_internet.check(task_dir, _config(task_dir)) == []


def test_closed_internet_rejects_a_short_justification(make_task):
    toml = _OPEN_TOML.replace(
        'description = "open task"',
        'description = "open task"\nopen_internet_justification = "TBD"',
    )
    task_dir = make_task(task_toml=toml)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_closed_internet_rejects_a_non_string_justification(make_task):
    # A list whose repr is long must not sneak past as a "justification".
    toml = _OPEN_TOML.replace(
        'description = "open task"',
        'description = "open task"\nopen_internet_justification = ["a", "b", "c", "d", "e", "f"]',
    )
    task_dir = make_task(task_toml=toml)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1


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


from oddish.preflight.checks import solution_format


def test_solution_format_flags_patch_files(make_task):
    task_dir = make_task(
        solve_sh="#!/bin/sh\ncp fix.py /app/\n",
        extra_files={"solution/fix.patch": "--- a\n+++ b\n"},
    )
    findings = solution_format.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "fix.patch" in findings[0].message


def test_solution_format_flags_diff_files(make_task):
    task_dir = make_task(
        solve_sh="#!/bin/sh\n",
        extra_files={"solution/fix.diff": "--- a\n+++ b\n"},
    )
    findings = solution_format.check(task_dir, _config(task_dir))
    assert len(findings) == 1


@pytest.mark.parametrize(
    "cmd",
    ["git apply fix.patch", "patch -p1 < fix.patch", "git am fix.patch"],
)
def test_solution_format_flags_patch_application_in_solve_sh(make_task, cmd):
    task_dir = make_task(solve_sh=f"#!/bin/sh\n{cmd}\n")
    findings = solution_format.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].line == 2


def test_solution_format_passes_a_readable_solution(make_task):
    task_dir = make_task(
        solve_sh="#!/bin/sh\ncp /solution/fix.py /app/fix.py\n",
        extra_files={"solution/fix.py": "print('fixed')\n"},
    )
    assert solution_format.check(task_dir, _config(task_dir)) == []


def test_solution_format_is_silent_without_a_solution_dir(make_task):
    task_dir = make_task()
    assert solution_format.check(task_dir, _config(task_dir)) == []

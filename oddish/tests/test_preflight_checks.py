from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.task.config import TaskConfig

from oddish.preflight.checks import dockerfile_leaks
from oddish.preflight.models import Severity


def _config(task_dir: Path) -> TaskConfig:
    return TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())


@pytest.mark.parametrize(
    "line",
    [
        # Literal file references — what harbor-lh's original caught.
        "COPY solution/solve.sh /app/solve.sh",
        "COPY tests/test.sh /app/test.sh",
        "COPY tests/test_main.py /app/",
        "ADD tests/test_helpers.py /opt/",
        # Directory and glob forms — the idiomatic way to write the same leak,
        # which the literal-path original silently passed.
        "COPY tests/ /app/tests",
        "COPY solution/ /app/solution",
        "COPY tests/test_*.py /app/",
        "COPY solution/*.sh /app/",
    ],
)
def test_dockerfile_leaks_flags_solution_and_test_references(make_task, line):
    task_dir = make_task(dockerfile=f"FROM ubuntu:24.04\n{line}\n")
    findings = dockerfile_leaks.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].check_id == "dockerfile_leaks"
    assert findings[0].severity is Severity.ERROR
    assert findings[0].line == 2


def test_dockerfile_leaks_passes_a_clean_dockerfile(make_task):
    task_dir = make_task(
        dockerfile="FROM ubuntu:24.04\nCOPY environment/app.py /app/\nWORKDIR /app\n"
    )
    assert dockerfile_leaks.check(task_dir, _config(task_dir)) == []


@pytest.mark.parametrize(
    "line",
    [
        # Patterns anchor on a path boundary, so a directory that merely ends
        # in "tests" or "solution" is not the task's tests/ or solution/ tree.
        "COPY unittests/test.sh /app/",
        "COPY my_solution/solve.sh /app/",
    ],
)
def test_dockerfile_leaks_does_not_false_positive_on_similar_names(make_task, line):
    task_dir = make_task(dockerfile=f"FROM ubuntu:24.04\n{line}\n")
    assert dockerfile_leaks.check(task_dir, _config(task_dir)) == []


def test_dockerfile_leaks_ignores_comments(make_task):
    task_dir = make_task(
        dockerfile="FROM ubuntu:24.04\n# COPY solution/solve.sh /app/\n"
    )
    assert dockerfile_leaks.check(task_dir, _config(task_dir)) == []


def test_dockerfile_leaks_is_silent_without_a_dockerfile(make_task):
    task_dir = make_task(dockerfile=None)
    assert dockerfile_leaks.check(task_dir, _config(task_dir)) == []


def test_dockerfile_leaks_reports_every_offending_line(make_task):
    task_dir = make_task(
        dockerfile=(
            "FROM ubuntu:24.04\n"
            "COPY solution/solve.sh /app/\n"
            "COPY tests/test.sh /app/\n"
        )
    )
    findings = dockerfile_leaks.check(task_dir, _config(task_dir))
    assert [f.line for f in findings] == [2, 3]

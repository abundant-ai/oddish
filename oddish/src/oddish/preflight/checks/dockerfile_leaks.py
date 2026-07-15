from __future__ import annotations

import re
from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "dockerfile_leaks"

# Referencing any of these from the agent's Dockerfile bakes the answer
# (the solution) or the grader (the tests) into the image the agent can read.
#
# Ported from harbor-lh's ci_checks/check-dockerfile-references.sh. That script
# is named for its mechanism rather than its purpose; the rename to
# `dockerfile_leaks` is deliberate.
_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"solution/solve\.sh"), "solution/solve.sh"),
    (re.compile(r"tests/test\.sh"), "tests/test.sh"),
    (re.compile(r"tests/test_[A-Za-z0-9_]*\.py"), "tests/test_*.py"),
)


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    dockerfile = task_dir / "environment" / "Dockerfile"
    if not dockerfile.is_file():
        return []

    findings: list[Finding] = []
    text = dockerfile.read_text(encoding="utf-8", errors="ignore")

    for lineno, line in enumerate(text.splitlines(), start=1):
        # Unlike the bash original, a commented-out reference is not a leak.
        if line.lstrip().startswith("#"):
            continue
        for pattern, label in _FORBIDDEN:
            if pattern.search(line):
                findings.append(
                    Finding(
                        check_id=CHECK_ID,
                        severity=Severity.ERROR,
                        task_dir=task_dir,
                        path=dockerfile,
                        line=lineno,
                        message=(
                            f"Dockerfile references {label}, which puts the "
                            "solution or the grader inside the agent's image."
                        ),
                        fix_hint=(
                            "Remove the reference. Harbor mounts tests at verify "
                            "time; the image must not contain them."
                        ),
                    )
                )
                break

    return findings

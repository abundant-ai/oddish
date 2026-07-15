from __future__ import annotations

import re
from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "dockerfile_leaks"

# Referencing any of these from the agent's Dockerfile bakes the answer
# (the solution) or the grader (the tests) into the image the agent can read.
#
# Ported from harbor-lh's ci_checks/check-dockerfile-references.sh, but
# deliberately broader. That script greps a fixed list of literal file paths,
# which its name ("references") is honest about — but it silently passes the
# idiomatic forms of the very leak it exists to prevent:
#
#     COPY tests/ /app/tests        -> ships the whole grader
#     COPY solution/ /app/solution  -> ships the whole answer
#     COPY tests/test_*.py /app/    -> literal glob, matches no literal path
#
# So the port matches the *directory* as well: any reference to the tests or
# solution tree, in any form.
#
# The anchor is `(?:^|[\s"'=])(?:\./)?` — start of line or a separator, with an
# optional `./`. It deliberately excludes `/` from the separator class: a
# leading slash would make the *destination* of `COPY x /app/tests/` match,
# which is not a leak. It also stops `unittests/` and `my_solution/` from
# false-positiving, since `t` and `_` are not separators.
_ANCHOR = r"(?:^|[\s\"'=])(?:\./)?"

_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(_ANCHOR + r"solution/"), "the solution/ tree"),
    (re.compile(_ANCHOR + r"tests/"), "the tests/ tree"),
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

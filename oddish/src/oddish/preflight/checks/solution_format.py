from __future__ import annotations

import re
from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "solution_format"

_PATCH_SUFFIXES = frozenset({".patch", ".diff"})
_PATCH_APPLY_RE = re.compile(r"\b(?:git\s+apply|git\s+am|patch\s+-p\d)\b")


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    solution_dir = task_dir / "solution"
    if not solution_dir.is_dir():
        return []

    findings: list[Finding] = []

    for path in sorted(solution_dir.rglob("*")):
        if path.is_file() and path.suffix in _PATCH_SUFFIXES:
            findings.append(
                Finding(
                    check_id=CHECK_ID,
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=path,
                    message=(
                        f"solution/ contains a patch file ({path.name}). "
                        "Solutions must be readable source, not diffs."
                    ),
                    fix_hint=(
                        "Ship the resulting source files and have solve.sh copy "
                        "or compile them."
                    ),
                )
            )

    solve_sh = solution_dir / "solve.sh"
    if solve_sh.is_file():
        text = solve_sh.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if _PATCH_APPLY_RE.search(line):
                findings.append(
                    Finding(
                        check_id=CHECK_ID,
                        severity=Severity.ERROR,
                        task_dir=task_dir,
                        path=solve_sh,
                        line=lineno,
                        message="solve.sh applies a patch instead of shipping source.",
                        fix_hint=(
                            "Replace the patch application with the post-fix "
                            "source files."
                        ),
                    )
                )

    return findings

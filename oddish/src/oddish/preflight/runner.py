from __future__ import annotations

from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Check, Finding, Severity
from oddish.preflight.registry import CHECKS


def run_checks(
    task_dirs: list[Path], checks: list[Check] | None = None
) -> list[Finding]:
    """Run every check against every task directory.

    task.toml is parsed once per task and handed to each check. A task whose
    config will not parse yields a single finding and is skipped: every check
    needs the config, so running them would just produce noise on top of the
    real problem.
    """
    active = CHECKS if checks is None else checks
    findings: list[Finding] = []

    for task_dir in task_dirs:
        config_path = task_dir / "task.toml"
        try:
            config = TaskConfig.model_validate_toml(config_path.read_text())
        except Exception as e:
            findings.append(
                Finding(
                    check_id="task_config",
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=config_path,
                    message=f"Could not parse task.toml: {type(e).__name__}: {e}",
                    fix_hint="Fix task.toml so Harbor can load the task.",
                )
            )
            continue

        for check in active:
            findings.extend(check.fn(task_dir, config))

    return findings


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity is Severity.ERROR for f in findings)

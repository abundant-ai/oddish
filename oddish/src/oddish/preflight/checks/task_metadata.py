from __future__ import annotations

from pathlib import Path
import tomllib

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "task_metadata"


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    """Check author declarations without treating Harbor defaults as declarations."""
    path = task_dir / "task.toml"
    raw = tomllib.loads(path.read_text())
    findings: list[Finding] = []

    if config.task is None:
        findings.append(
            Finding(
                check_id=CHECK_ID,
                severity=Severity.ERROR,
                task_dir=task_dir,
                path=path,
                message="Missing [task].name declaration.",
                fix_hint=f'Add [task] with name = "abundant/{task_dir.name}".',
            )
        )
    elif config.task.name.rsplit("/", 1)[-1] != task_dir.name:
        findings.append(
            Finding(
                check_id=CHECK_ID,
                severity=Severity.ERROR,
                task_dir=task_dir,
                path=path,
                message=(
                    f"Task name {config.task.name!r} does not match directory "
                    f"{task_dir.name!r}."
                ),
                fix_hint="Make the name after the optional org/ prefix match the task directory.",
            )
        )

    reward_type = config.metadata.get("reward_type")
    if not isinstance(reward_type, str) or not reward_type.strip():
        findings.append(
            Finding(
                check_id=CHECK_ID,
                severity=Severity.ERROR,
                task_dir=task_dir,
                path=path,
                message="Missing or empty [metadata].reward_type declaration.",
                fix_hint="Declare reward_type as a nonempty string describing the task's reward type.",
            )
        )

    # Phase overrides inherit their baseline. A separate verifier environment
    # has its own baseline and must declare it, including on multi-step tasks.
    environments = [("environment", raw.get("environment", {}))]
    verifier = raw.get("verifier", {})
    if "environment" in verifier:
        environments.append(("verifier.environment", verifier["environment"]))
    for step in raw.get("steps", []):
        step_verifier = step.get("verifier", {})
        if "environment" in step_verifier:
            environments.append(
                (
                    f"steps.{step['name']}.verifier.environment",
                    step_verifier["environment"],
                )
            )

    for section, environment in environments:
        if "network_mode" not in environment and "allow_internet" not in environment:
            findings.append(
                Finding(
                    check_id=CHECK_ID,
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=path,
                    message=f"Missing explicit internet-access declaration in [{section}].",
                    fix_hint=(
                        "Declare network_mode as public, no-network, or allowlist. "
                        "Legacy allow_internet booleans are also accepted."
                    ),
                )
            )
        elif "network_mode" not in environment and not isinstance(
            environment["allow_internet"], bool
        ):
            findings.append(
                Finding(
                    check_id=CHECK_ID,
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=path,
                    message=f"[{section}].allow_internet must be a boolean.",
                    fix_hint="Use true or false without quotes, or declare network_mode.",
                )
            )

        if any(not gpu.strip() for gpu in environment.get("gpu_types", [])):
            findings.append(
                Finding(
                    check_id=CHECK_ID,
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=path,
                    message=f"[{section}].gpu_types contains a blank GPU type.",
                    fix_hint="Supply nonempty GPU type names, or omit gpu_types when unrestricted.",
                )
            )

    return findings

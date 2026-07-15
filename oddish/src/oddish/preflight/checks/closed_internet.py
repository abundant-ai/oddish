from __future__ import annotations

from pathlib import Path

from harbor.models.task.config import NetworkMode, TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "closed_internet"

_JUSTIFICATION_KEY = "open_internet_justification"
_MIN_JUSTIFICATION_CHARS = 20

# Lowercased exact matches that are not justifications.
_PLACEHOLDERS = frozenset(
    {"tbd", "todo", "n/a", "na", "none", "placeholder", "xxx", "fixme", "-"}
)


def _is_real_justification(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < _MIN_JUSTIFICATION_CHARS:
        return False
    return stripped.lower() not in _PLACEHOLDERS


def _effective(
    phase_mode: NetworkMode | None, baseline: NetworkMode
) -> NetworkMode:
    """A phase with no explicit mode inherits the environment baseline."""
    return baseline if phase_mode is None else phase_mode


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    baseline = config.environment.network_mode
    phases = {
        "environment baseline": baseline,
        "[agent]": _effective(config.agent.network_mode, baseline),
        "[verifier]": _effective(config.verifier.network_mode, baseline),
    }

    open_phases = [
        name for name, mode in phases.items() if mode is NetworkMode.PUBLIC
    ]
    if not open_phases:
        return []

    justification = str(config.metadata.get(_JUSTIFICATION_KEY) or "")
    if _is_real_justification(justification):
        return []

    where = ", ".join(open_phases)
    return [
        Finding(
            check_id=CHECK_ID,
            severity=Severity.ERROR,
            task_dir=task_dir,
            path=task_dir / "task.toml",
            message=(
                f"Open internet ({where}) with no justification. An agent with "
                "network access can fetch the upstream repo regardless of what "
                "the image ships."
            ),
            fix_hint=(
                'Set network_mode = "no-network" (or "allowlist" with '
                "allowed_hosts), or add a [metadata] "
                f"{_JUSTIFICATION_KEY} of at least "
                f"{_MIN_JUSTIFICATION_CHARS} characters explaining why."
            ),
        )
    ]

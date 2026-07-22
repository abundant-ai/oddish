from __future__ import annotations

from pathlib import Path

from harbor.models.task.config import NetworkMode, TaskConfig
from harbor.models.task.verifier_mode import (
    resolve_step_verifier_mode,
    resolve_task_verifier_mode,
)
from harbor.models.trial.config import AgentConfig as TrialAgentConfig
from harbor.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
from harbor.trial.network_policy import resolve_trial_network_plan

from oddish.preflight.models import Finding, Severity

CHECK_ID = "closed_internet"

_JUSTIFICATION_KEY = "open_internet_justification"
_MIN_JUSTIFICATION_CHARS = 20

# Neutral trial-side configs: no run-specific agent identity, no extra allowed
# hosts. They exist only because Harbor's resolver takes them; with empty
# extra_allowed_hosts they contribute nothing, leaving the task.toml as the sole
# source of the effective policy.
_NEUTRAL_AGENT = TrialAgentConfig()
_NEUTRAL_ENV = TrialEnvironmentConfig()


def _is_real_justification(value: object) -> bool:
    # Only a genuine written string counts. A non-string (list/number/bool)
    # whose repr happens to be long must not slip through.
    if not isinstance(value, str):
        return False
    return len(value.strip()) >= _MIN_JUSTIFICATION_CHARS


def _open_phases(config: TaskConfig) -> list[str]:
    """Every phase Harbor will actually run whose effective policy is PUBLIC.

    Delegates to Harbor's own resolver so we never re-derive inheritance,
    per-step overrides, or the separate-verifier-container baseline by hand.
    """
    steps = config.steps or [None]
    labels: list[str] = []

    for step in steps:
        mode = (
            resolve_task_verifier_mode(config)
            if step is None
            else resolve_step_verifier_mode(config, step)
        )
        plan = resolve_trial_network_plan(
            config, _NEUTRAL_AGENT, _NEUTRAL_ENV, step, verifier_mode=mode
        )
        suffix = "" if step is None else f" (step {step.name!r})"
        candidates = [
            (f"[environment]{suffix}", plan.agent_env_baseline),
            (f"[agent]{suffix}", plan.agent_phase),
            (f"[verifier]{suffix}", plan.verifier_phase),
        ]
        if plan.verifier_env_baseline is not None:
            candidates.append(
                (f"[verifier.environment]{suffix}", plan.verifier_env_baseline)
            )
        for label, policy in candidates:
            if policy.network_mode is NetworkMode.PUBLIC:
                labels.append(label)

    # De-dupe while keeping first-seen order.
    return list(dict.fromkeys(labels))


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    open_phases = _open_phases(config)
    if not open_phases:
        return []

    if _is_real_justification(config.metadata.get(_JUSTIFICATION_KEY)):
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
                "allowed_hosts) on the open phase(s), or add a [metadata] "
                f"{_JUSTIFICATION_KEY} of at least "
                f"{_MIN_JUSTIFICATION_CHARS} characters explaining why."
            ),
        )
    ]

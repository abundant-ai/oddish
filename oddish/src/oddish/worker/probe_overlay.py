"""Pure helpers for the probe instruction overlay.

Kept free of DB/S3/filesystem side effects so the rendering and trial
selection logic can be unit-tested directly. ``local_runner`` wires these
into the live trial: it stages related-trial logs to disk and renders the
mutated ``instruction.md``.
"""

from __future__ import annotations

from typing import Any, Protocol


class _TrialLike(Protocol):
    id: str
    harbor_config: dict[str, Any] | None


# Always-appended guidance. Test discovery is intentionally fuzzy: across
# real tasks the verifier is named inconsistently (run_tests.sh, tests/test.sh,
# test.sh, run_tests.py, ...), so we describe the convention and let the agent
# locate the actual script rather than hard-coding a path.
_RUNNING_TESTS_SECTION = """## RUNNING TESTS

To check your work against the task's verifier, run its test script. The exact
name and location vary by task — most commonly `bash run_tests.sh` or
`bash tests/test.sh` at the task root, but it may be named `test.sh`,
`testing.sh`, or `run_tests.py`. Don't assume a fixed path: list the task root
and search for the verifier (e.g. files matching `*test*.sh` or `run_tests*`),
run it, and iterate against the reward it reports."""


def _related_logs_section(related_dir: str, has_related: bool) -> str:
    if not has_related:
        return (
            "## RELATED TRIAL LOGS\n\n"
            "No prior non-probe attempts were available to stage for this task."
        )
    return (
        "## RELATED TRIAL LOGS\n\n"
        f"Logs from prior real (non-probe) attempts at this task have been "
        f"staged read-only under `{related_dir}`. Each subdirectory is one "
        "trial and contains that run's agent transcript and result/reward. "
        "Use them as reference — to see how earlier agents approached the "
        "task, where they passed or failed, and what the verifier rewarded. "
        "They are copies; editing them has no effect on scoring."
    )


def render_probe_instruction(
    framing: str,
    directive: str,
    original: str,
    *,
    related_dir: str,
    has_related: bool,
) -> str:
    """Render the full mutated ``instruction.md`` for a probe trial.

    Layout: system framing, the operator directive (the real goal), the
    original task instruction (context only), then the two always-appended
    sections (running tests + related trial logs).
    """
    return (
        f"{framing}\n\n"
        f"---\n\n"
        f"## OPERATOR DIRECTIVE\n\n{directive}\n\n"
        f"---\n\n"
        f"## ORIGINAL TASK INSTRUCTION (context only)\n\n{original}\n\n"
        f"---\n\n"
        f"{_RUNNING_TESTS_SECTION}\n\n"
        f"---\n\n"
        f"{_related_logs_section(related_dir, has_related)}"
    )


def select_related_trials(
    trials: list[_TrialLike],
    *,
    current_trial_id: str,
) -> list[_TrialLike]:
    """Pick the "real attempt" siblings whose logs are worth staging.

    Excludes the current trial and any other probe-mode trial
    (``harbor_config.mode == "probe"``), leaving genuine solution attempts.
    """
    selected = []
    for trial in trials:
        if trial.id == current_trial_id:
            continue
        config = getattr(trial, "harbor_config", None) or {}
        if config.get("mode") == "probe":
            continue
        selected.append(trial)
    return selected

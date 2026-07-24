"""Prompt text for the trajectory-summary TrajectoryBlock. Kept apart from the
block logic so prompt edits don't touch parsing/validation."""
from __future__ import annotations

PREAMBLE = (
    "You are summarizing a recorded agent trajectory for a developer who wants "
    "a quick scan before diving into the per-step view."
)


def task_section(task_name: str, instruction: str) -> str:
    return f"<task>\nName: {task_name}\nInstruction: {instruction}\n</task>"


def outcome_section(final_reward: str, verifier_output: str, model_used: str) -> str:
    return (
        "<outcome>\n"
        f"Final reward: {final_reward}\n"
        f"Verifier output: {verifier_output}\n"
        f"Model: {model_used}\n"
        "</outcome>"
    )


def instructions_section(template: str, taxonomy_values: list[str]) -> str:
    # str.replace, not .format: the template body contains JSON braces.
    return template.replace("{{taxonomy}}", ", ".join(taxonomy_values))


def trajectory_section(trajectory_json: str) -> str:
    return f"<trajectory>\n{trajectory_json}\n</trajectory>"


def trajectory_file_section(path: str, step_count: int) -> str:
    """Point a filesystem-capable agent at the trajectory instead of inlining it.

    Trajectories reach ~350k tokens at the tail of the distribution, which does
    not fit a prompt. Handing over a path lets the agent read the file in
    chunks and keeps the prompt a fixed small size regardless of trial length.
    """
    return (
        "<trajectory>\n"
        f"The ATIF trajectory for this trial is on disk at {path}\n"
        f"It contains {step_count} steps under the top-level \"steps\" key; each "
        'step carries an integer "step_id".\n'
        "Read it (in chunks if large) before segmenting. Every step_id you "
        "reference must come from that file.\n"
        "</trajectory>"
    )

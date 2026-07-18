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


def instructions_section(taxonomy_values: list[str]) -> str:
    labels = ", ".join(taxonomy_values)
    return (
        "Produce a 2-3 sentence summary covering what the agent set out to do, "
        "how it ended, and whether the verifier agreed. Then 3-6 pivotal "
        "'highlights' with their step ids.\n\n"
        "Each highlight must reference a real `step_id` from the trajectory "
        "below. Pick steps where something genuinely shifted.\n\n"
        "Also segment the run into COMPONENTS: assign EVERY step to exactly one "
        "component whose `trajectory_component` is one of these labels: "
        f"{labels}. Give each component the step ids it covers and a "
        "one-sentence summary. Cover all steps in order.\n\n"
        "Respond with ONLY a JSON object (no preamble, no code fences) matching "
        "this exact shape:\n"
        "{\n"
        '  "summary": "2-3 sentences",\n'
        '  "highlights": [\n'
        '    {"step_id": <int>, "title": "<short label>", "why": "<one sentence>"}\n'
        "  ],\n"
        '  "components": [\n'
        '    {"step_ids": [<int>, ...], "trajectory_component": "<label>", '
        '"summary": "<one sentence>"}\n'
        "  ]\n"
        "}\n"
        "Highlights must be ordered by step_id ascending."
    )


def trajectory_section(trajectory_json: str) -> str:
    return f"<trajectory>\n{trajectory_json}\n</trajectory>"

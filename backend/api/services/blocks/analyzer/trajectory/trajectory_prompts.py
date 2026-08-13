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


# One phrase per label. The model gets no other guidance on what a label
# means, so this text is the whole definition. Keyed by value rather than
# enum member to keep this module free of the block's imports; every member
# must appear here or render_taxonomy raises.
TAXONOMY_DESCRIPTIONS: dict[str, str] = {
    "reading_files": "opens, lists, or searches files to see what is there.",
    "thinking_recall": (
        "restates known facts, requirements, or findings from earlier in this run."
    ),
    "thinking_understand": (
        "works out how existing code or an observed failure actually behaves."
    ),
    "thinking_hypothesize": (
        "proposes a cause or an outcome that is not yet confirmed."
    ),
    "writing_plan": (
        "sets out intended work before that work is done. Forward-looking only."
    ),
    "plan_correction": (
        "abandons or materially changes a plan stated earlier in this run, and "
        "adopts a different approach. Needs an earlier plan to revise."
    ),
    "implementing": (
        "writes or edits code, configuration, or files toward the solution."
    ),
    "implementing_correction": (
        "repairs the agent's own earlier edit, such as a compile error, a wrong "
        "import, or a bad value."
    ),
    "writing_tests": "adds or edits tests.",
    "testing_public": "runs the task's provided tests or checker.",
    "testing_custom": "runs tests or scripts that the agent wrote itself.",
    "testing_edge_cases": "deliberately exercises boundary or unusual inputs.",
    "debugging": (
        "investigates a failure that already occurred, such as reading an error, "
        "adding logging, or bisecting."
    ),
    "writing_report": (
        "reports on work already done, such as a status write-up, a hand-off "
        "message, or a final claim that the task is complete. Backward-looking, "
        "where `writing_plan` is forward-looking."
    ),
}

EXPLORE_HEADING = (
    "THINKING / EXPLORING -- the agent is learning, and the solution does not change:"
)
IMPLEMENT_HEADING = (
    "IMPLEMENTING / TESTING -- the agent is changing the solution or checking it:"
)


def render_taxonomy(explore_values: list[str], implement_values: list[str]) -> str:
    """Render the grouped, defined vocabulary the model chooses labels from.

    Raises on a value with no description: a label the enum offers but the
    prompt never defines is worse than a missing label, because the model
    still has to use it and can only guess from the name.
    """
    missing = [
        v
        for v in (*explore_values, *implement_values)
        if v not in TAXONOMY_DESCRIPTIONS
    ]
    if missing:
        raise ValueError(f"taxonomy labels without a description: {missing}")

    def block(heading: str, values: list[str]) -> str:
        lines = [heading]
        lines += [f"- `{v}`: {TAXONOMY_DESCRIPTIONS[v]}" for v in values]
        return "\n".join(lines)

    return (
        block(EXPLORE_HEADING, explore_values)
        + "\n\n"
        + block(IMPLEMENT_HEADING, implement_values)
    )


def instructions_section(
    template: str, explore_values: list[str], implement_values: list[str]
) -> str:
    # str.replace, not .format: the template body contains JSON braces.
    return template.replace(
        "{{taxonomy}}", render_taxonomy(explore_values, implement_values)
    )


def trajectory_section(trajectory_json: str) -> str:
    return f"<trajectory>\n{trajectory_json}\n</trajectory>"

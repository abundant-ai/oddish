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

# Precedence rules. The vocabulary mixes two axes -- some labels name an ACTION
# (`reading_files`: opens, lists, searches) and some name a PURPOSE
# (`debugging`: investigates a failure). A step that greps a file to chase a
# stack trace satisfies both definitions completely, and every step must take
# exactly one label, so without a stated precedence the choice is a coin flip.
#
# Measured on 45 trials re-summarized twice (8,098 steps): 17.9% of steps
# changed side of the explore/implement boundary between two runs of identical
# input. `debugging` <-> `reading_files` was 52% of those crossings and
# `testing_custom` <-> `thinking_understand` another 17%, so these two rules
# target ~69% of the instability. `debugging` alone reproduced on 37.2% of its
# steps.
TAXONOMY_PRECEDENCE = """When two labels both fit, apply these rules in order:
1. A step that investigates a failure that already happened is `debugging`,
   even when it opens, searches, or reads files to do it. Use `reading_files`
   only when no specific failure is being chased.
2. A step that runs an agent-written script to learn how something behaves is
   `thinking_understand`. Use `testing_custom` only when the run checks whether
   the agent's own solution is correct.
3. Use `plan_correction` only when the approach changes. An agent that fixes
   code without reconsidering its approach is doing `implementing_correction`.
4. Prefer the more specific label when two fit, and prefer a label from the
   group that matches what the step changed: if the solution did not change,
   choose from the exploring group."""


# The two axes the flat vocabulary conflates. Asking for them separately is
# what actually removes the `debugging` / `reading_files` collision: that step
# is action=read, purpose=diagnose, and neither answer has to lose.
#
# `action` is close to mechanical -- it follows from the step's tool calls --
# so it anchors the harder `purpose` judgement to something observable.
ACTION_DESCRIPTIONS: dict[str, str] = {
    "read": "opens, lists, searches, or greps something to see its contents.",
    "edit": "writes or changes a file.",
    "run": "executes a command, script, test, or checker.",
    "prose": "reasons or reports without using a tool.",
}

PURPOSE_DESCRIPTIONS: dict[str, str] = {
    "understand": "to learn how something already behaves.",
    "plan": "to decide what the agent will do next. Forward-looking.",
    "build": "to move the solution toward done.",
    "verify": "to check whether the solution is correct.",
    "diagnose": "to find the cause of a failure that already happened.",
}

AXES_HEADING = (
    "Also give every component an `action` and a `purpose`. These are separate "
    "questions from `trajectory_component`: answer each on its own, and do not "
    "let one constrain the other. A step can read files in order to diagnose."
)


def render_axes() -> str:
    """Render the two-axis vocabulary that accompanies the flat label."""
    lines = [AXES_HEADING, "", "`action` -- what the step physically did:"]
    lines += [f"- `{k}`: {v}" for k, v in ACTION_DESCRIPTIONS.items()]
    lines += ["", "`purpose` -- what the step was for:"]
    lines += [f"- `{k}`: {v}" for k, v in PURPOSE_DESCRIPTIONS.items()]
    return "\n".join(lines)


def taxonomy_fingerprint() -> str:
    """Short hash over the label semantics the model is actually shown.

    Freshness for stored summaries keyed on ``schema_version`` alone means a
    change that alters what a label MEANS, without altering the response
    shape, leaves every cached summary serving the old vocabulary forever --
    live data still contains `thinking_diagnose` and
    `testing_custom_edge_cases`, labels the enum no longer offers. Mixing
    vocabularies silently breaks any comparison across time.

    Covers the descriptions, the group headings, and the precedence rules,
    because each of those changes how a step gets labelled. Deliberately NOT
    the whole instructions template: a typo fix in unrelated prompt prose
    should not invalidate ~72k paid summaries.
    """
    import hashlib

    payload = "\n".join(
        [
            *(f"{k}={TAXONOMY_DESCRIPTIONS[k]}" for k in sorted(TAXONOMY_DESCRIPTIONS)),
            *(f"action:{k}={ACTION_DESCRIPTIONS[k]}" for k in sorted(ACTION_DESCRIPTIONS)),
            *(f"purpose:{k}={PURPOSE_DESCRIPTIONS[k]}" for k in sorted(PURPOSE_DESCRIPTIONS)),
            EXPLORE_HEADING,
            IMPLEMENT_HEADING,
            AXES_HEADING,
            TAXONOMY_PRECEDENCE,
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


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
        + "\n\n"
        + TAXONOMY_PRECEDENCE
    )


def instructions_section(
    template: str, explore_values: list[str], implement_values: list[str]
) -> str:
    # str.replace, not .format: the template body contains JSON braces.
    return template.replace(
        "{{taxonomy}}",
        render_taxonomy(explore_values, implement_values) + "\n\n" + render_axes(),
    )


def trajectory_section(trajectory_json: str) -> str:
    return f"<trajectory>\n{trajectory_json}\n</trajectory>"

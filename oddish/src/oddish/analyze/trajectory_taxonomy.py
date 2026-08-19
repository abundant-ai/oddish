"""Trajectory-summary component vocabulary and its prompt rendering.

Ported from ``backend/api/services/blocks/analyzer/trajectory`` (the
trajectory block deleted with the old analysis pipeline) so the QA brief and
the importer share one definition of the label set, the precedence rules
(#1272), and the version fingerprint that keys summary freshness. The text
here IS the vocabulary: the model gets no other guidance on what a label
means.
"""

from __future__ import annotations

import enum
import hashlib

# The stored trajectory_summary payload shape. Bump when the response shape
# changes; the vocabulary itself is fingerprinted separately by
# ``taxonomy_version`` below.
SCHEMA_VERSION = "6"


class ExploreTrajectoryBlockTaxonomy(str, enum.Enum):
    # These two groups are not only documentary: the prompt renders them as
    # its own headings, so membership here is what the model is told the
    # label means. Planning lives on this side because it is thinking about
    # the work, not doing it -- PLAN_CORRECTION sits beside WRITING_PLAN so
    # the two read as a pair.
    READING_FILES = "reading_files"
    THINKING_RECALL = "thinking_recall"
    THINKING_UNDERSTAND = "thinking_understand"
    THINKING_HYPOTHESIZE = "thinking_hypothesize"
    WRITING_PLAN = "writing_plan"
    PLAN_CORRECTION = "plan_correction"


class ImplementTrajectoryBlockTaxonomy(str, enum.Enum):
    # Ordered as the work happens: build, correct, test, debug, report.
    # WRITING_REPORT is last because it is what a run ends with.
    IMPLEMENTING = "implementing"
    IMPLEMENTING_CORRECTION = "implementing_correction"
    WRITING_TESTS = "writing_tests"
    TESTING_PUBLIC = "testing_public"
    TESTING_CUSTOM = "testing_custom"
    TESTING_EDGE_CASES = "testing_edge_cases"
    DEBUGGING = "debugging"
    WRITING_REPORT = "writing_report"


# One flat vocabulary built from the two sub-enums, so a component carries a
# single taxonomy value. Built from the members above to avoid value drift.
TrajectoryBlockTaxonomy = enum.Enum(
    "TrajectoryBlockTaxonomy",
    {
        m.name: m.value
        for m in (*ExploreTrajectoryBlockTaxonomy, *ImplementTrajectoryBlockTaxonomy)
    },
    type=str,
)


class ActionAxis(str, enum.Enum):
    """What the step physically did. Nearly mechanical: follows the tool calls."""

    READ = "read"
    EDIT = "edit"
    RUN = "run"
    PROSE = "prose"


class PurposeAxis(str, enum.Enum):
    """What the step was for. The judgement the flat vocabulary muddles."""

    UNDERSTAND = "understand"
    PLAN = "plan"
    BUILD = "build"
    VERIFY = "verify"
    DIAGNOSE = "diagnose"


# One phrase per label. The model gets no other guidance on what a label
# means, so this text is the whole definition. Every member of the flat
# vocabulary must appear here or render_taxonomy raises.
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

# Precedence rules. The vocabulary mixes two axes -- some labels name an
# ACTION (`reading_files`: opens, lists, searches) and some name a PURPOSE
# (`debugging`: investigates a failure). A step that greps a file to chase a
# stack trace satisfies both definitions completely, and every step must take
# exactly one label, so without a stated precedence the choice is a coin
# flip. See #1272 for the reproducibility numbers behind each rule.
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


def render_taxonomy() -> str:
    """Render the grouped, defined vocabulary the model chooses labels from.

    Raises on a label with no description: a label the enum offers but the
    prompt never defines is worse than a missing label, because the model
    still has to use it and can only guess from the name.
    """
    explore_values = [m.value for m in ExploreTrajectoryBlockTaxonomy]
    implement_values = [m.value for m in ImplementTrajectoryBlockTaxonomy]
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


def render_summary_instructions(template: str) -> str:
    """Fill the trajectory-summary template's ``{{taxonomy}}`` slot.

    str.replace, not .format: the template body contains JSON braces.
    """
    return template.replace(
        "{{taxonomy}}", render_taxonomy() + "\n\n" + render_axes()
    )


def taxonomy_version() -> str:
    """Short hash over the label semantics the model is actually shown.

    Freshness for stored summaries keyed on ``schema_version`` alone means a
    change that alters what a label MEANS, without altering the response
    shape, leaves every cached summary serving the old vocabulary forever.
    Covers the descriptions, the group headings, the axes, and the precedence
    rules, because each of those changes how a step gets labelled.
    """
    payload = "\n".join(
        [
            *(f"{k}={TAXONOMY_DESCRIPTIONS[k]}" for k in sorted(TAXONOMY_DESCRIPTIONS)),
            *(
                f"action:{k}={ACTION_DESCRIPTIONS[k]}"
                for k in sorted(ACTION_DESCRIPTIONS)
            ),
            *(
                f"purpose:{k}={PURPOSE_DESCRIPTIONS[k]}"
                for k in sorted(PURPOSE_DESCRIPTIONS)
            ),
            EXPLORE_HEADING,
            IMPLEMENT_HEADING,
            AXES_HEADING,
            TAXONOMY_PRECEDENCE,
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]

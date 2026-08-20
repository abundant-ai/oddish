"""Deterministic activity summary for an analysis trial's own trajectory.

QA and audit trials (``trials.kind`` != 'agent') summarize the trials they
grade; nothing summarizes the analysis run itself, so its Activity panel went
blank when on-demand generation was removed with the block pipeline. Workers
execute no LLM calls of their own (AGENTS.md), and an analysis run is
stereotyped, so a tool-call scan labels every observed step without claiming
that a failed partial run reached the fetch or artifact-write stages. Counted,
not judged, by the same rule as :mod:`trajectory_delegation` and
:mod:`trajectory_provenance`.

The vocabulary here is analysis-shaped, not the solver taxonomy the QA agent
applies to graded trials (:mod:`trajectory_taxonomy`): a run that never edits
a solution has no use for ``implementing``. The frontend renders labels it
does not know by de-snaking the value, so these need no client change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from oddish.analyze.trajectory_delegation import SUBAGENT_TOOL_NAMES
from oddish.analyze.trajectory_tool_calls import (
    COMMAND_ARGUMENT_KEYS,
    PATH_ARGUMENT_KEYS,
    string_argument,
    string_arguments,
    tool_name,
)

ANALYSIS_ACTIVITY_VERSION = "analysis-activity:v1"


@dataclass(frozen=True)
class AnalysisActivityRule:
    label: str
    description: str
    action: str
    purpose: str
    gist: str
    observed_action: str


# Order is mixed-tool precedence. Writing the artifact wins because the
# importer depends on it. The same rows own the stored component definitions,
# axes, per-component gists, and whole-run prose.
ANALYSIS_ACTIVITY_RULES = (
    AnalysisActivityRule(
        "writing_result",
        "writes the analysis result artifact under /logs.",
        "edit",
        "build",
        "Wrote the result artifact.",
        "It wrote {artifact_name}.",
    ),
    AnalysisActivityRule(
        "fetching_trial_data",
        "runs the oddish-query CLI to fetch task and trial data.",
        "run",
        "understand",
        "Fetched task and trial data with oddish-query.",
        "It fetched task and trial data with the oddish-query CLI.",
    ),
    AnalysisActivityRule(
        "delegating",
        "dispatches a subagent.",
        "run",
        "understand",
        "Dispatched a subagent.",
        "It dispatched a subagent.",
    ),
    AnalysisActivityRule(
        "reading_files",
        "opens, lists, or searches the fetched files.",
        "read",
        "understand",
        "Read and searched the fetched files.",
        "It read or searched files.",
    ),
    AnalysisActivityRule(
        "inspecting_data",
        "runs shell commands or unrecognized tools over the available data.",
        "run",
        "understand",
        "Inspected data with tools.",
        "It inspected data with tools.",
    ),
    AnalysisActivityRule(
        "reasoning",
        "reasons or plans without touching the data.",
        "prose",
        "plan",
        "Reasoned without tools.",
        "It reasoned without tools.",
    ),
)
_RULE_BY_LABEL = {rule.label: rule for rule in ANALYSIS_ACTIVITY_RULES}

_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_RUN_TOOLS = frozenset({"Bash"})
# TodoWrite tracks the agent's own plan; it touches no task or trial data.
_PLANNING_TOOLS = frozenset({"TodoWrite"})

# A trial can be mentioned by hundreds of steps in a long run; the anchor list
# exists to jump a reader to the relevant stretch, not to mirror the scan.
_MAX_MENTION_STEPS = 50


def _classify_call(call: dict) -> str:
    name = tool_name(call)
    if name in SUBAGENT_TOOL_NAMES:
        return "delegating"
    if name in _WRITE_TOOLS:
        path = string_argument(call, PATH_ARGUMENT_KEYS) or ""
        return "writing_result" if path.startswith("/logs") else "inspecting_data"
    if name in _RUN_TOOLS:
        command = string_argument(call, COMMAND_ARGUMENT_KEYS) or ""
        return "fetching_trial_data" if "oddish-query" in command else "inspecting_data"
    if name in _READ_TOOLS:
        return "reading_files"
    if name in _PLANNING_TOOLS:
        return "reasoning"
    # An unrecognized tool still did something to the data; the honest bucket
    # is the generic one, never a specific claim.
    return "inspecting_data"


def classify_step(step: dict) -> str:
    calls = step.get("tool_calls") or []
    labels = {_classify_call(call) for call in calls if isinstance(call, dict)}
    for rule in ANALYSIS_ACTIVITY_RULES:
        if rule.label in labels:
            return rule.label
    return "reasoning"


def build_analysis_activity_summary(
    *,
    kind: str,
    task_name: str | None,
    trial_count: int,
    status: str,
    artifact_name: str,
    trajectory: dict,
) -> dict | None:
    """The ``{summary, highlights, components}`` base an importer enriches.

    Components are contiguous same-label runs, matching how the model-based
    summaries segment a solver run, so the Activity card's instance bars and
    timeline render identically. Returns None when the trajectory has no
    usable steps.
    """
    steps = [
        step
        for step in (trajectory or {}).get("steps") or []
        if isinstance(step, dict) and isinstance(step.get("step_id"), int)
    ]
    if not steps:
        return None

    components: list[dict] = []
    for step in steps:
        label = classify_step(step)
        if components and components[-1]["trajectory_component"] == label:
            components[-1]["step_ids"].append(step["step_id"])
            continue
        rule = _RULE_BY_LABEL[label]
        components.append(
            {
                "step_ids": [step["step_id"]],
                "trajectory_component": label,
                "action": rule.action,
                "purpose": rule.purpose,
                "summary": rule.gist,
            }
        )

    task = f"task `{task_name}`" if task_name else "its task"
    if kind == "qa":
        opening = (
            f"This QA trial audited {trial_count} agent "
            f"trial{'s' if trial_count != 1 else ''} of {task}."
        )
    elif kind == "audit":
        opening = f"This {kind} trial reviewed the source of {task}."
    else:
        opening = f"This {kind} trial analyzed one trial of {task}."
    observed_labels: set[str] = set()
    observed_actions: list[str] = []
    for component in components:
        label = component["trajectory_component"]
        if label in observed_labels:
            continue
        observed_labels.add(label)
        observed_actions.append(
            _RULE_BY_LABEL[label].observed_action.format(artifact_name=artifact_name)
        )
    summary_text = " ".join(
        [opening, *observed_actions, f"The run finished {status.upper()}."]
    )

    highlights: list[dict] = []
    first_fetch = next(
        (
            c["step_ids"][0]
            for c in components
            if c["trajectory_component"] == "fetching_trial_data"
        ),
        None,
    )
    if first_fetch is not None:
        highlights.append(
            {
                "step_id": first_fetch,
                "title": "First data fetch",
                "why": "The agent started to pull task and trial data here.",
            }
        )
    last_write = next(
        (
            c["step_ids"][-1]
            for c in reversed(components)
            if c["trajectory_component"] == "writing_result"
        ),
        None,
    )
    if last_write is not None:
        highlights.append(
            {
                "step_id": last_write,
                "title": f"Wrote {artifact_name}",
                "why": "The importer parses the artifact this step wrote.",
            }
        )
    highlights.sort(key=lambda h: h["step_id"])

    return {
        "summary": summary_text,
        "highlights": highlights,
        "components": components,
        # Marks the summary as counted by this module rather than produced by
        # a model, so a reader of the stored payload can tell which rules made
        # it (the exact rule set is fingerprinted in ``taxonomy_version``).
        "generator": "analysis-activity",
    }


def trial_mention_steps(
    trajectory: dict | None, trial_ids: list[str]
) -> dict[str, list[int]]:
    """``trial_id -> step_ids of the analysis run that name it``.

    A mention is the trial id appearing in any string argument of a tool call
    — an oddish-query command, or a path under the fetched data. Purely a
    scan, so the anchors stored on a graded trial can never cite steps that
    do not exist. Ids shorter than a real trial id are skipped rather than
    fuzzy-matched.
    """
    if not isinstance(trajectory, dict):
        return {}
    wanted = [t for t in trial_ids if isinstance(t, str) and len(t) >= 8]
    if not wanted:
        return {}
    patterns = {
        trial_id: re.compile(
            rf"(?<![A-Za-z0-9_-]){re.escape(trial_id)}(?![A-Za-z0-9_-])"
        )
        for trial_id in wanted
    }
    out: dict[str, list[int]] = {}
    for step in trajectory.get("steps") or []:
        if not isinstance(step, dict) or not isinstance(step.get("step_id"), int):
            continue
        haystacks = [
            value
            for call in step.get("tool_calls") or []
            if isinstance(call, dict)
            for value in string_arguments(call)
        ]
        if not haystacks:
            continue
        for trial_id, pattern in patterns.items():
            if any(pattern.search(text) for text in haystacks):
                mentions = out.setdefault(trial_id, [])
                if len(mentions) < _MAX_MENTION_STEPS:
                    mentions.append(step["step_id"])
    return out

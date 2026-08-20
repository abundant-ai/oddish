"""Deterministic activity summary for an analysis trial's own trajectory.

QA and audit trials (``trials.kind`` != 'agent') summarize the trials they
grade; nothing summarizes the analysis run itself, so its Activity panel went
blank when on-demand generation was removed with the block pipeline. Workers
execute no LLM calls of their own (AGENTS.md), and an analysis run is
stereotyped — fetch data with oddish-query, read it, reason, write one
artifact — so a tool-call scan labels every step. Counted, not judged, by the
same rule as :mod:`trajectory_delegation` and :mod:`trajectory_provenance`.

The vocabulary here is analysis-shaped, not the solver taxonomy the QA agent
applies to graded trials (:mod:`trajectory_taxonomy`): a run that never edits
a solution has no use for ``implementing``. The frontend renders labels it
does not know by de-snaking the value, so these need no client change.
"""

from __future__ import annotations

import hashlib

from oddish.analyze.trajectory_delegation import SUBAGENT_TOOL_NAMES

# One phrase per label, mirroring TAXONOMY_DESCRIPTIONS: the description is
# the definition, and the fingerprint below covers it so a change in what a
# label means changes the stored ``taxonomy_version``.
ANALYSIS_COMPONENT_DESCRIPTIONS: dict[str, str] = {
    "fetching_trial_data": "runs the oddish-query CLI to fetch task and trial data.",
    "reading_files": "opens, lists, or searches the fetched files.",
    "inspecting_data": "runs shell commands over the fetched data.",
    "delegating": "dispatches a subagent.",
    "writing_result": "writes the analysis result artifact under /logs.",
    "reasoning": "reasons or plans without touching the data.",
}

# When one step holds differently-classified tool calls it takes the label
# earliest in this list. Writing the artifact outranks everything: it is the
# one step the importer depends on.
_STEP_PRECEDENCE = (
    "writing_result",
    "fetching_trial_data",
    "delegating",
    "reading_files",
    "inspecting_data",
    "reasoning",
)

# (action, purpose) axes per label, same vocabularies as trajectory_taxonomy.
_AXES: dict[str, tuple[str, str]] = {
    "fetching_trial_data": ("run", "understand"),
    "reading_files": ("read", "understand"),
    "inspecting_data": ("run", "understand"),
    "delegating": ("run", "understand"),
    "writing_result": ("edit", "build"),
    "reasoning": ("prose", "plan"),
}

_GISTS: dict[str, str] = {
    "fetching_trial_data": "Fetched task and trial data with oddish-query.",
    "reading_files": "Read and searched the fetched files.",
    "inspecting_data": "Inspected data with shell commands.",
    "delegating": "Dispatched a subagent.",
    "writing_result": "Wrote the result artifact.",
    "reasoning": "Reasoned without tools.",
}

_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_RUN_TOOLS = frozenset({"Bash"})
# TodoWrite tracks the agent's own plan; it touches no task or trial data.
_PLANNING_TOOLS = frozenset({"TodoWrite"})

# Argument keys holding a shell command or a path, matching
# trajectory_provenance's key lists for the same agents.
_COMMAND_KEYS = ("command", "cmd", "script", "shell_command")
_PATH_KEYS = ("file_path", "filePath", "path", "filename", "file")

# A trial can be mentioned by hundreds of steps in a long run; the anchor list
# exists to jump a reader to the relevant stretch, not to mirror the scan.
_MAX_MENTION_STEPS = 50


def _tool_name(call: object) -> str | None:
    """``function_name``, falling back to ``name``. Mirrors delegation.py."""
    if not isinstance(call, dict):
        return None
    return call.get("function_name") or call.get("name")


def _str_args(call: dict) -> list[str]:
    args = call.get("arguments")
    if not isinstance(args, dict):
        return []
    return [v for v in args.values() if isinstance(v, str)]


def _arg(call: dict, keys: tuple[str, ...]) -> str | None:
    args = call.get("arguments")
    if not isinstance(args, dict):
        return None
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _classify_call(call: dict) -> str:
    name = _tool_name(call)
    if name in SUBAGENT_TOOL_NAMES:
        return "delegating"
    if name in _WRITE_TOOLS:
        path = _arg(call, _PATH_KEYS) or ""
        return "writing_result" if path.startswith("/logs") else "inspecting_data"
    if name in _RUN_TOOLS:
        command = _arg(call, _COMMAND_KEYS) or ""
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
    for label in _STEP_PRECEDENCE:
        if label in labels:
            return label
    return "reasoning"


def analysis_activity_version() -> str:
    """Fingerprint of these label semantics, stored as ``taxonomy_version``.

    Same role as ``trajectory_taxonomy.taxonomy_version``: a summary counted
    under an older rule set must not read as fresh after the rules change.
    """
    payload = "\n".join(
        f"{k}={ANALYSIS_COMPONENT_DESCRIPTIONS[k]}"
        for k in sorted(ANALYSIS_COMPONENT_DESCRIPTIONS)
    )
    return "counted:" + hashlib.sha256(payload.encode()).hexdigest()[:12]


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
        action, purpose = _AXES[label]
        components.append(
            {
                "step_ids": [step["step_id"]],
                "trajectory_component": label,
                "action": action,
                "purpose": purpose,
                "summary": _GISTS[label],
            }
        )

    task = f"task `{task_name}`" if task_name else "its task"
    if kind == "qa":
        opening = (
            f"This QA trial audited {trial_count} agent "
            f"trial{'s' if trial_count != 1 else ''} of {task}."
        )
    else:
        opening = f"This {kind} trial reviewed the source of {task}."
    summary_text = (
        f"{opening} It fetched data with the oddish-query CLI and wrote "
        f"{artifact_name}. The run finished {status.upper()}."
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
    out: dict[str, list[int]] = {}
    for step in trajectory.get("steps") or []:
        if not isinstance(step, dict) or not isinstance(step.get("step_id"), int):
            continue
        haystacks = [
            value
            for call in step.get("tool_calls") or []
            if isinstance(call, dict)
            for value in _str_args(call)
        ]
        if not haystacks:
            continue
        for trial_id in wanted:
            if any(trial_id in text for text in haystacks):
                mentions = out.setdefault(trial_id, [])
                if len(mentions) < _MAX_MENTION_STEPS:
                    mentions.append(step["step_id"])
    return out

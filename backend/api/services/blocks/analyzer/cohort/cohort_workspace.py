"""The filesystem a cohort comparison reads.

The comparison used to run as one API call whose whole world was the rendered
prompt: component label, step range, summary prose. It could not look at what
an agent actually did. Inlining the trajectories is not an option -- they run
to a median 1.1 MB (~263k tokens) and a max of 3.3 MB -- so instead the trials
are laid out on disk, one file per trajectory component, and claude-code reads
what it needs with Read/Glob/Grep.

One file per component rather than one per trial: a component is already the
bounded unit the comparison reasons in, and its span keeps the working set
small. `MAP.md` is the index that makes the files findable -- the "map, not a
sandbox" shape the post-trial classifier settled on.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from api.services.blocks.analyzer.cohort.cohort_prompts import short_model_name

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def component_filename(index: int, label: str, first: int, last: int) -> str:
    """`03-testing_public-10-42.json`.

    The ordinal prefix keeps two components that share a label and span from
    colliding, and sorts the directory in trajectory order. The label is
    scrubbed because it reaches us as stored text -- retired taxonomy labels
    are still in the data, and a future one could carry a path separator.
    """
    safe = _UNSAFE.sub("_", label or "component")
    return f"{index:02d}-{safe}-{first}-{last}.json"


def _content_text(value) -> str:
    """Message content is a bare string or a list of typed blocks."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def step_text(step) -> str:
    """Everything quotable in one step, flattened.

    This is what a step-level citation is checked against, so it has to cover
    every place the agent's own words can live: the message, its reasoning, the
    tool calls it made (name and arguments), and what came back.
    """
    if not isinstance(step, dict):
        return ""
    parts: list[str] = []
    message = _content_text(step.get("message"))
    if message:
        parts.append(message)
    reasoning = step.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        parts.append(reasoning)
    for call in step.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = call.get("function_name") or call.get("name")
        if name:
            parts.append(str(name))
        arguments = call.get("arguments")
        if isinstance(arguments, (dict, list)):
            parts.append(json.dumps(arguments, ensure_ascii=False))
        elif isinstance(arguments, str):
            parts.append(arguments)
    observation = step.get("observation")
    if isinstance(observation, dict):
        for result in observation.get("results") or []:
            if isinstance(result, dict):
                content = _content_text(result.get("content"))
                if content:
                    parts.append(content)
    return "\n".join(parts)


def _steps_by_id(trajectory: dict) -> dict[int, dict]:
    return {
        step["step_id"]: step
        for step in (trajectory or {}).get("steps") or []
        if isinstance(step, dict) and isinstance(step.get("step_id"), int)
    }


def _map_section(label: str, trials: list[dict], written: dict) -> list[str]:
    lines = [f"## {label}"]
    if not trials:
        lines.append("(no runs on this side)")
        return lines
    for trial in trials:
        tid = trial["trial_id"]
        bits = [f"### {tid}"]
        # The same shortener the prompt uses. The agent reads both the map and
        # the prompt, so a raw `global.anthropic.claude-opus-4-8` here beside a
        # shortened one there reads as two models.
        model = short_model_name(trial.get("model") or "")
        if model:
            bits.append(f"model: {model}")
        delegation = trial.get("delegation")
        if isinstance(delegation, dict):
            dispatches = delegation.get("dispatches")
            bits.append(
                f"subagents: {dispatches if delegation.get('capable') else 'n/a'}"
            )
        lines.append(" | ".join(bits))
        files = written.get(tid) or {}
        if not files:
            lines.append("  (no trajectory on disk -- summaries only)")
        for position, component in enumerate(trial.get("components") or []):
            ids = component.get("step_ids") or []
            if not ids:
                continue
            span = f"[{min(ids)}-{max(ids)}]"
            name = files.get(position)
            where = f" -> trials/{tid}/{name}" if name else ""
            lines.append(
                f"  - {component.get('trajectory_component')} {span}"
                f"{where}\n    {(component.get('summary') or '').strip()}"
            )
    return lines


def build_workspace(
    root: Path,
    *,
    task_name: str,
    successful: list[dict],
    failing: list[dict],
    trajectories: dict[str, dict],
) -> dict[tuple[str, int], str]:
    """Lay the cohorts out under ``root``; return the step index for validation.

    The index covers EVERY step in each fetched trajectory, not only the ones a
    component claimed: the summariser drops inert steps and long runs are only
    partly covered, so a citation can legitimately name a step no component
    owns. Validation still has to be able to check it.

    A trial whose trajectory could not be fetched is simply absent from disk;
    the comparison degrades to summaries for that trial rather than failing.
    """
    root = Path(root)
    index: dict[tuple[str, int], str] = {}
    written: dict[str, dict[int, str]] = {}

    for trial in [*successful, *failing]:
        tid = trial["trial_id"]
        trajectory = trajectories.get(tid)
        if not isinstance(trajectory, dict):
            continue
        by_id = _steps_by_id(trajectory)
        for step_id, step in by_id.items():
            index[(tid, step_id)] = step_text(step)

        trial_dir = root / "trials" / tid
        trial_dir.mkdir(parents=True, exist_ok=True)
        names: dict[int, str] = {}
        for position, component in enumerate(trial.get("components") or []):
            ids = [i for i in (component.get("step_ids") or []) if isinstance(i, int)]
            if not ids:
                continue
            first, last = min(ids), max(ids)
            # The whole span, not just the cited ids: the point is to give the
            # reader of this file the run as it happened, gaps included.
            steps = [by_id[i] for i in sorted(by_id) if first <= i <= last]
            name = component_filename(
                position, component.get("trajectory_component") or "", first, last
            )
            (trial_dir / name).write_text(
                json.dumps(
                    {
                        "trial_id": tid,
                        "trajectory_component": component.get("trajectory_component"),
                        "step_ids": [first, last],
                        "summary": component.get("summary"),
                        "steps": steps,
                    },
                    ensure_ascii=False,
                    indent=1,
                )
            )
            names[position] = name
        written[tid] = names

    lines = [f"# Cohort comparison: {task_name}", ""]
    lines += _map_section("SUCCESSFUL", successful, written)
    lines.append("")
    lines += _map_section("FAILING", failing, written)
    root.mkdir(parents=True, exist_ok=True)
    (root / "MAP.md").write_text("\n".join(lines) + "\n")
    return index

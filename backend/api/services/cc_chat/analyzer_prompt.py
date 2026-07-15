"""Build the per-cohort agent prompt for the sandboxed analyzer.

Pure: no I/O beyond reading the packaged prompt templates, no Daytona. Assembled
by concatenation rather than one f-string because the map template contains
literal ``{placeholder}`` braces that would break f-string parsing.
"""

from __future__ import annotations

import json
from importlib.resources import files

from oddish.evals.analyzer.prompt_builder import (
    SECTION_KEYS_BY_BUCKET,
    sections_block,
)
from oddish.evals.primitives import SubAnalysis

# Matches ClaudeCodeRuntime's cwd, so the agent can use absolute paths from
# any working directory.
WORKSPACE = "/home/daytona/workspace"
CLI_DEST = f"{WORKSPACE}/oddish-query"
OUT_DIR = f"{WORKSPACE}/out"
FINDINGS_PATH = f"{OUT_DIR}/findings.jsonl"
REDUCE_PATH = f"{OUT_DIR}/reduce.json"


def _prompts_dir():
    return files("oddish.evals.analyzer") / "prompts"


def _cohort_block(bucket: str, cohort: list[SubAnalysis], oracle_by_trial: dict[str, str]) -> str:
    out = []
    for sa in cohort:
        lines = [
            f"- trial_id: {sa.trial_id}",
            f"  classification: {sa.classification}   subtype: {sa.subtype}",
            f"  prior_evidence: {sa.evidence}",
            f"  prior_root_cause: {sa.root_cause}",
            f"  trajectory_link: {sa.trajectory_link}",
        ]
        # Oracle context is bad-bucket-only; gate structurally so a caller
        # mistake can't leak it into a good-cohort prompt.
        oracle = oracle_by_trial.get(sa.trial_id) if bucket == "bad" else None
        if oracle:
            lines.append(f"  oracle_context: {oracle}")
        out.append("\n".join(lines))
    return "\n".join(out)


def _roster_block(roster: list[dict]) -> str:
    return "\n".join(
        f"- {r['trial_id']} [{r['bucket']}/{r['subtype']}] {r['trajectory_link']}"
        for r in roster
    )


def build_cohort_prompt(
    bucket: str,
    cohort: list[SubAnalysis],
    roster: list[dict],
    counts: dict,
    oracle_by_trial: dict[str, str],
) -> str:
    # map.txt is a str.format() template; its literal JSON braces are escaped
    # ({{/}}) for that path. We inline it raw for the agent, so unescape them
    # here — leaving the single-brace {placeholder}s for the agent to fill.
    map_template = (
        (_prompts_dir() / "map.txt").read_text().replace("{{", "{").replace("}}", "}")
    )
    section_keys = SECTION_KEYS_BY_BUCKET[bucket]
    example = json.dumps({k: "...markdown..." for k in section_keys})

    intro = (
        f"You are running the '{bucket}' half of an agent-eval failure analysis\n"
        "over a cohort of trials, using a MAP -> REDUCE process. You do NOT have\n"
        "the trajectories yet — pull each one yourself with the oddish-query CLI.\n\n"
        "## Tool: oddish-query CLI (run it with the Bash tool)\n"
        "Fetch a trial's full trajectory with:\n"
        f"    node {CLI_DEST} trials logs <trial_id> --trajectory\n"
        "Output is JSONL (head/tail truncated at ~4KB each end — expected). Every\n"
        "line is prefixed with a PROBE-ONLY banner; ignore it.\n\n"
        "## Full roster (BOTH cohorts — for cross-referencing only)\n"
        f"{_roster_block(roster)}\n\n"
        f"## Your cohort ({len(cohort)} trials) — the ONLY trials to analyze\n"
        f"{_cohort_block(bucket, cohort, oracle_by_trial)}\n"
    )

    # Non-f strings below: they reference the template's literal {placeholders}.
    map_phase = (
        "\n## PHASE 1 — MAP (one finding per trial)\n"
        "For EACH trial in your cohort, in order:\n"
        "  1. Fetch its trajectory with the CLI command above.\n"
        "  2. Fill in the MAP template below from that trajectory + the trial's\n"
        "     cohort metadata.\n"
        "  3. Emit the finding as a single JSON object on its own line, prefixed\n"
        f"     with `MAP FINDING:`, AND append that same line to {FINDINGS_PATH}.\n\n"
        "When filling the MAP template: {bucket} = " + bucket + "; {classification},\n"
        "{subtype}, {evidence}, {root_cause}, {oracle_context} come from the cohort\n"
        "metadata above ({oracle_context} is empty when not listed — skip it);\n"
        "{trajectory_link} must be copied VERBATIM from the metadata;\n"
        "{trajectory_block} and {roster_block} you derive from the fetched\n"
        "trajectory and the roster.\n\n"
        "--- MAP template ---\n"
        f"{map_template}\n"
    )

    reduce_phase = (
        "\n## PHASE 2 — REDUCE (synthesize ONCE, after ALL maps)\n"
        "You are the lead analyst synthesizing the per-trial findings. Every claim\n"
        "MUST cite specific trials by embedding the finding's trajectory_link\n"
        "verbatim as a markdown link, e.g.\n"
        '"...hardcoded the output ([trajectory](/tasks/t1/probe/x))...".\n\n'
        "## Counts\n"
        f"{json.dumps(counts, indent=2)}\n\n"
        f"## Write these markdown sections:\n{sections_block(section_keys)}\n\n"
        "## Output\n"
        f"Emit the result as a single JSON object prefixed with `REDUCE RESULT:`,\n"
        f"with exactly these keys:\n{example}\n\n"
        "## REQUIRED final action\n"
        f"Your LAST action must be writing that same JSON object to {REDUCE_PATH}\n"
        f"(create {OUT_DIR} first). The file is how your work is collected — if it\n"
        "is missing, the analysis fails.\n\n"
        "Work through trials one at a time and narrate as you go so it streams.\n"
        "Finish the MAP phase completely before starting REDUCE.\n"
    )
    return intro + map_phase + reduce_phase

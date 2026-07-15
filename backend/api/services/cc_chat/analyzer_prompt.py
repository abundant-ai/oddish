"""Build the per-cohort agent prompt for the sandboxed analyzer.

Pure: no I/O beyond reading the packaged prompt templates, no Daytona. Assembled
by concatenation rather than one f-string because the shared fragments contain
literal ``{placeholder}`` braces that would break f-string parsing.
"""

from __future__ import annotations

import json

from oddish.evals.analyzer.prompt_builder import (
    SECTION_KEYS_BY_BUCKET,
    map_output_shape,
    map_rubric,
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
    # map_output_shape() is escaped ({{/}}) for the API path's str.format(); we
    # unescape it for the agent, leaving {trajectory_link} for it to fill in.
    output_shape = map_output_shape().replace("{{", "{").replace("}}", "}")
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

    # Non-f strings below: output_shape carries a literal {trajectory_link}.
    map_phase = (
        "\n## PHASE 1 — MAP (one finding per trial)\n"
        "For EACH trial in your cohort, in order:\n"
        "  1. Fetch its trajectory with the CLI command above.\n"
        "  2. Classify it using the rubric below (an emergent label is fine if\n"
        "     none of the seed subcategories fit).\n"
        "  3. Emit the finding as a single JSON object on its own line, prefixed\n"
        f"     with `MAP FINDING:`, AND append that same line to {FINDINGS_PATH}.\n"
        "     Copy trajectory_link VERBATIM from the cohort metadata above — never\n"
        "     invent it.\n\n"
        "## Subcategory rubric (seed; you MAY introduce an emergent label if none fit)\n"
        + map_rubric() + "\n\n"
        "## Finding shape\n"
        + output_shape + "\n\n"
        # map.txt states this rule against a pre-loaded trajectory ("above"); here
        # the agent fetches its own, so the wording differs but the rule is the same.
        "Use the `step_id` values exactly as they appear in the trajectory you\n"
        "fetched for `step_ids` — these are ids, not positions in the list.\n\n"
        "Narrate as you go so it streams.\n"
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

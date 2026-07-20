"""Build the per-cohort agent prompt for the sandboxed analyzer.

Pure: no I/O beyond reading the packaged prompt templates, no Daytona. Assembled
by concatenation rather than one f-string because the shared fragments contain
literal ``{placeholder}`` braces that would break f-string parsing.
"""

from __future__ import annotations

import json

from oddish.evals.analyzer.prompt_builder import (
    SECTION_KEYS_BY_BUCKET,
    by_model_output_shape,
    denominators_block,
    map_output_shape,
    map_rubric,
    sections_block,
    task_roster_block,
)
from oddish.evals.analyzer.taxonomy import Taxonomy
from oddish.evals.primitives import SubAnalysis

# Matches ClaudeCodeRuntime's cwd, so the agent can use absolute paths from
# any working directory.
WORKSPACE = "/home/daytona/workspace"
CLI_DEST = f"{WORKSPACE}/oddish-query"
OUT_DIR = f"{WORKSPACE}/out"
FINDINGS_PATH = f"{OUT_DIR}/findings.jsonl"
REDUCE_PATH = f"{OUT_DIR}/reduce.json"

# One findings file PER BATCH, not one shared file.
#
# Batches used to append to a single findings.jsonl, which made correctness
# depend on every agent choosing `>>` over `>`. They do not: a real 97-trial run
# used a truncating `>` 32 times and the file went 10 -> 30 -> 10 lines as later
# batches wiped earlier ones. 80 findings were emitted and 47 survived -- and
# nothing failed, because reduce happily synthesized from what was left.
#
# Per-batch files make that unrepresentable: an agent can only ever clobber its
# own file, and the host concatenates. No shared mutable state, so no
# instruction for an agent to disobey.
FINDINGS_GLOB = f"{OUT_DIR}/findings-*.jsonl"


def findings_path(batch_no: int) -> str:
    # Zero-padded so the reduce agent's `cat findings-*.jsonl` walks batches in
    # the same order the host concatenates them; unpadded, 10 sorts before 2.
    return f"{OUT_DIR}/findings-{batch_no:02d}.jsonl"


def build_system_prompt(tail_bytes: int) -> str:
    """Counter the pull toward under-fetching.

    `trials logs --trajectory` returns only the LAST ``tail_bytes`` of a
    trajectory. A truncated trajectory still reads as complete and coherent, so
    an agent will happily analyze the tail and never notice the setup that
    explains it -- and that failure is silent: the finding looks fine, it is just
    shallower than the evidence supports. The inline truncation banner alone is
    easy to skim past, so the permission to widen is restated here, where it
    holds for every turn.
    """
    return (
        "You are analyzing agent trials inside a sandbox, using the oddish-query "
        "CLI (via the Bash tool) as your window onto the data.\n\n"
        f"IMPORTANT — trajectories are truncated. `trials logs <id> --trajectory` "
        f"shows only the LAST {tail_bytes} bytes by default, because failures "
        "usually surface at the end of a run. That is a default, NOT a limit.\n\n"
        "PULL MORE WHENEVER YOU NEED IT. You have full, unmetered access to this "
        "CLI — use it freely and repeatedly:\n"
        f"  - `trials logs <id> --trajectory --tail-bytes {tail_bytes * 5}` (or "
        "larger) to see earlier steps.\n"
        "  - Re-run with a bigger budget the moment the tail alone does not "
        "explain the failure.\n"
        "  - Run `--help` to see every command available to you.\n\n"
        "A truncated trajectory reads as if it were whole. If a root cause "
        "depends on something you cannot see — setup, an earlier decision, the "
        "original instruction — that is not a reason to guess or to hedge. Fetch "
        "more and then decide. Never state a root cause you could have verified "
        "by pulling more of the trajectory."
    )


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


def build_map_batch_prompt(
    bucket: str,
    batch: list[SubAnalysis],
    roster: list[dict],
    oracle_by_trial: dict[str, str],
    batch_no: int,
    batch_total: int,
    tail_bytes: int,
    taxonomy: Taxonomy,
) -> str:
    """MAP over one batch only. Each batch runs in a FRESH claude process, so
    this prompt must stand alone -- the agent has no memory of prior batches.
    ``findings.jsonl`` on disk is the only thing carried across them.
    """
    output_shape = map_output_shape().replace("{{", "{").replace("}}", "}")
    return (
        f"You are running the '{bucket}' half of an agent-eval failure analysis,\n"
        f"MAP phase, batch {batch_no} of {batch_total}. You do NOT have the\n"
        "trajectories yet — pull each one yourself with the oddish-query CLI.\n\n"
        "## Tool: oddish-query CLI (run it with the Bash tool)\n"
        "Fetch a trial's trajectory with:\n"
        f"    node {CLI_DEST} trials logs <trial_id> --trajectory\n"
        f"Output is JSONL showing the LAST {tail_bytes} bytes of the trajectory —\n"
        "these runs are far longer than that, so you are seeing the end of the run,\n"
        "not all of it. Add `--tail-bytes N` to see more. Every line is prefixed\n"
        "with a PROBE-ONLY banner; ignore it.\n\n"
        "## Full roster (BOTH cohorts — for cross-referencing only)\n"
        f"{_roster_block(roster)}\n\n"
        f"## Your batch ({len(batch)} trials) — the ONLY trials to analyze now\n"
        f"{_cohort_block(bucket, batch, oracle_by_trial)}\n"
        "\n## MAP (one finding per trial)\n"
        "For EACH trial in your batch, in order:\n"
        "  1. Fetch its trajectory with the CLI command above.\n"
        "  2. Classify it using the rubric below (an emergent label is fine if\n"
        "     none of the seed subcategories fit).\n"
        "  3. Report the finding TWICE, in two different forms:\n"
        "     a. To the transcript: `MAP FINDING: {...}` — prefix, then the JSON.\n"
        f"     b. To {findings_path(batch_no)}: the SAME JSON object as ONE line of\n"
        "        RAW JSON — NO `MAP FINDING:` prefix, no code fence, no\n"
        "        commentary. The line must start with `{` and be parseable by\n"
        "        json.loads on its own; a prefixed line is silently discarded and\n"
        "        that trial's finding is lost.\n"
        f"        Create {OUT_DIR} first. This file is YOURS alone (batch\n"
        f"        {batch_no}); append to it as you go so a finding is never lost,\n"
        "        and by the end it must hold exactly one line per trial in your\n"
        "        batch.\n"
        "     Copy trajectory_link VERBATIM from the batch metadata above — never\n"
        "     invent it.\n\n"
        "## Subcategory rubric (seed; you MAY introduce an emergent label if none fit)\n"
        + map_rubric(taxonomy) + "\n\n"
        "## Finding shape\n"
        + output_shape + "\n\n"
        "Use the `step_id` values exactly as they appear in the trajectory you\n"
        "fetched for `step_ids` — these are ids, not positions in the list.\n\n"
        "Do NOT synthesize or summarize across trials — that is a later step.\n"
        "Narrate as you go so it streams.\n"
    )


def build_reduce_only_prompt(
    bucket: str,
    counts: dict,
    batch_total: int,
    models_by_task: dict[str, list[str]] | None = None,
    denominators: dict[str, dict] | None = None,
) -> str:
    """REDUCE over findings.jsonl. Runs in its own fresh claude process, so it
    reads the findings back off disk rather than holding them in context -- this
    is what keeps reduce independent of cohort size.
    """
    section_keys = SECTION_KEYS_BY_BUCKET[bucket]
    # Unescape the shared fragment: it is str.format-escaped for the API path,
    # but this prompt is assembled by concatenation, not .format().
    by_model_example = by_model_output_shape().replace("{{", "{").replace("}}", "}")
    # One JSON object, not two concatenated ones: the section keys stay open
    # (no json.dumps here) so by_model_example's keys close the same brace.
    section_pairs = ", ".join(f'"{k}": "...markdown..."' for k in section_keys)
    example = f"{{{section_pairs},\n {by_model_example}}}"
    # Only the good bucket owns headroom_analysis, and it is the only brief that
    # asks for the roster. Rendering it for the bad cohort would hand that agent
    # a block no section it writes can use.
    roster_section = (
        "## Task roster — which models RAN each task, including the ones that PASSED\n"
        f"{task_roster_block(models_by_task)}\n\n"
        if "headroom_analysis" in section_keys
        else ""
    )
    denominators_section = (
        "## Per-model counts — the denominators for every per-model claim\n"
        f"{denominators_block(denominators)}\n\n"
        if denominators
        else ""
    )
    return (
        f"You are the lead analyst for the '{bucket}' half of an agent-eval\n"
        "failure analysis. The MAP phase is OVER: it ran "
        f"{batch_total} batch(es) of per-trial findings, one file per batch,\n"
        f"one JSON object per line.\n\n"
        "## Step 1 — read the findings\n"
        f"Read them ALL with the Bash tool:\n"
        # 2>/dev/null: bash has no nullglob here, so an all-batches-failed glob
        # reaches cat as a literal path and it reports "No such file or
        # directory". That is the empty case, not an error to retry or escalate.
        f"    cat {FINDINGS_GLOB} 2>/dev/null\n"
        f"Read the glob, never a single file: up to {batch_total} such files may\n"
        "exist, and a batch that failed is simply absent. Synthesize from whatever\n"
        "findings you do get, and note the reduced coverage in your sections. Those\n"
        "findings are your ONLY input — do NOT re-fetch trajectories. If that\n"
        "command prints nothing at all, there are no findings: say so and stop.\n\n"
        "## Step 2 — synthesize\n"
        "Every claim MUST cite specific trials by embedding the finding's\n"
        "trajectory_link verbatim as a markdown link, e.g.\n"
        '"...hardcoded the output ([trajectory](/tasks/t1/probe/x))...".\n\n'
        "## Counts\n"
        f"{json.dumps(counts, indent=2)}\n\n"
        f"{roster_section}"
        f"{denominators_section}"
        f"## Write these markdown sections:\n{sections_block(section_keys)}\n\n"
        "## Then write the per-model breakdown\n"
        "Emit one by_model entry for EVERY model in the per-model counts above,\n"
        "even if it has no findings — \"this model produced no failures of this\n"
        "kind\" is a result. Use ONLY model ids that appear in those counts.\n\n"
        "## Output\n"
        f"Emit the result as a single JSON object prefixed with `REDUCE RESULT:`,\n"
        f"with exactly these keys:\n{example}\n\n"
        "## REQUIRED final action\n"
        f"Your LAST action must be writing that same JSON object to {REDUCE_PATH}\n"
        f"(create {OUT_DIR} first). The file is how your work is collected — if it\n"
        "is missing, the analysis fails.\n"
    )


def build_cohort_prompt(
    bucket: str,
    cohort: list[SubAnalysis],
    roster: list[dict],
    counts: dict,
    oracle_by_trial: dict[str, str],
    taxonomy: Taxonomy,
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
        + map_rubric(taxonomy) + "\n\n"
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

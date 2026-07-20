from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from oddish.evals.primitives import SubAnalysis, TrajectoryBundle
from oddish.evals.analyzer.bucketing import BUCKET_OF
from oddish.evals.analyzer.schemas import Finding

_PROMPT_DIR = Path(__file__).parent / "prompts"
MAP_PROMPT_TEMPLATE = (_PROMPT_DIR / "map.txt").read_text()
REDUCE_PROMPT_TEMPLATE = (_PROMPT_DIR / "reduce.txt").read_text()

_SECTIONS_DIR = _PROMPT_DIR / "sections"
_FRAGMENTS_DIR = _PROMPT_DIR / "fragments"

# Order matches reduce.txt's original bullet list; sections_block depends on it.
SECTION_KEYS: tuple[str, ...] = (
    "bad_failure_content",
    "good_failure_content",
    "universal_capabilities_content",
    "headroom_analysis",
)

# Which cohort owns which sections. reduce.txt derives bad_failure_content from
# the bad cohort and the other three from "the good failures", so a per-cohort
# run needs no cross-cohort synthesis.
SECTION_KEYS_BY_BUCKET: dict[str, tuple[str, ...]] = {
    "bad": ("bad_failure_content",),
    "good": (
        "good_failure_content",
        "universal_capabilities_content",
        "headroom_analysis",
    ),
}


def section_brief(key: str) -> str:
    return (_SECTIONS_DIR / f"{key}.txt").read_text().rstrip("\n")


def sections_block(keys: Sequence[str]) -> str:
    return "\n".join(section_brief(k) for k in keys)


def map_rubric() -> str:
    return (_FRAGMENTS_DIR / "map_rubric.txt").read_text().rstrip("\n")


def map_output_shape() -> str:
    """The finding's JSON shape, still str.format-escaped ({{ }}) and still
    carrying the {trajectory_link} placeholder. Callers choose: .format() it
    (API path) or unescape it and let the agent fill the link (sandbox path)."""
    return (_FRAGMENTS_DIR / "map_output.txt").read_text().rstrip("\n")


def by_model_output_shape() -> str:
    """The per-model output keys, still str.format-escaped ({{ }}). Shared by
    both reduce paths so the API and sandbox schemas cannot drift."""
    return (_FRAGMENTS_DIR / "by_model_output.txt").read_text().rstrip("\n")


def denominators_block(denominators: dict[str, dict] | None) -> str:
    """Per-model trial counts. Findings record only failures, so without these
    denominators a model that simply ran more trials reads as the worse model.

    ``scored`` is the denominator for ``solved`` and mean reward: reward is
    nullable (no test result) and supports partial credit, so trials is wrong
    for both.
    """
    if not denominators:
        return "(no per-model denominators available)"
    lines = []
    for model, d in sorted(denominators.items()):
        mean = d.get("mean_reward")
        mean_txt = "mean reward n/a" if mean is None else f"mean reward {mean:.2f}"
        lines.append(
            f"- {model}: {d.get('trials', 0)} trials, "
            f"{d.get('solved', 0)} solved of {d.get('scored', 0)} scored, "
            f"{mean_txt}; {d.get('analyzed', 0)} analyzed "
            f"({d.get('bad', 0)} bad, {d.get('good', 0)} good)"
        )
    return "\n".join(lines)


def _trajectory_block(bundle: TrajectoryBundle) -> str:
    summary = json.dumps(bundle.trajectory_summary or {}, indent=2)
    steps = json.dumps(bundle.trajectory, indent=2)
    logs = json.dumps(bundle.logs, indent=2)
    return f"summary:\n{summary}\n\nsteps:\n{steps}\n\nlogs:\n{logs}"


def _roster_block(roster: list[dict]) -> str:
    return "\n".join(
        f"- {r['trial_id']} [{r['bucket']}/{r['subtype']}] {r['trajectory_link']}"
        for r in roster
    )


def build_map_prompt(
    bundle: TrajectoryBundle, subanalysis: SubAnalysis, roster: list[dict]
) -> str:
    return MAP_PROMPT_TEMPLATE.format(
        trial_id=bundle.trial_id,
        bucket=BUCKET_OF.get(subanalysis.classification, "other"),
        classification=subanalysis.classification,
        subtype=subanalysis.subtype,
        evidence=subanalysis.evidence,
        root_cause=subanalysis.root_cause,
        trajectory_link=bundle.trajectory_link,
        oracle_context=bundle.oracle_context or "(none — not a reward-hacking trial)",
        trajectory_block=_trajectory_block(bundle),
        roster_block=_roster_block(roster),
        rubric_block=map_rubric(),
        output_block=map_output_shape().format(trajectory_link=bundle.trajectory_link),
    )


def _reduce_findings_block(findings: list[Finding]) -> str:
    return "\n".join(
        f"- [{f.bucket}/{f.subcategory}] trial={f.trial_id} link={f.trajectory_link}\n"
        f"  task: {f.task_path or f.task_id or 'unknown'}\n"
        f"  model: {f.model or 'unknown'}\n"
        f"  quote: {f.evidence_quote}\n  root_cause: {f.root_cause}\n"
        f"  headroom_signal: {f.headroom_signal}"
        for f in findings
    )


def task_roster_block(models_by_task: dict[str, list[str]] | None) -> str:
    """Which models RAN each task, including the ones that PASSED. Findings
    record only failures, so this is the sole source for "every model passed" --
    without it, saturated and too-hard are indistinguishable to the synthesizer.

    None means no roster was persisted (pre-analyzers_006); it must not collapse
    into the empty case, which is the real answer "no trials"."""
    if models_by_task is None:
        return "(no roster persisted for this run — pass/fail coverage unknown)"
    if not models_by_task:
        return "(no trials)"
    return "\n".join(
        f"- {task}: {', '.join(sorted(models)) or '(none)'}"
        for task, models in sorted(models_by_task.items())
    )


def build_reduce_prompt(
    findings: list[Finding],
    counts: dict,
    models_by_task: dict[str, list[str]] | None = None,
    denominators: dict[str, dict] | None = None,
) -> str:
    return REDUCE_PROMPT_TEMPLATE.format(
        counts_block=json.dumps(counts, indent=2),
        roster_block=task_roster_block(models_by_task),
        denominators_block=denominators_block(denominators),
        findings_block=_reduce_findings_block(findings),
        sections_block=sections_block(SECTION_KEYS),
        # by_model_output_shape() is escaped ({{ }}) for reuse by the sandbox
        # path too; unescape here since this call's output goes straight to
        # the LLM as the final prompt, not through another .format() pass.
        by_model_block=by_model_output_shape().format(),
    )

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


def build_reduce_prompt(findings: list[Finding], counts: dict) -> str:
    findings_block = "\n".join(
        f"- [{f.bucket}/{f.subcategory}] trial={f.trial_id} link={f.trajectory_link}\n"
        f"  quote: {f.evidence_quote}\n  root_cause: {f.root_cause}\n"
        f"  headroom_signal: {f.headroom_signal}"
        for f in findings
    )
    counts_block = json.dumps(counts, indent=2)
    return REDUCE_PROMPT_TEMPLATE.format(
        counts_block=counts_block,
        findings_block=findings_block,
        sections_block=sections_block(SECTION_KEYS),
    )

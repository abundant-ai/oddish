from __future__ import annotations

import json
from pathlib import Path

from oddish.evals.primitives import SubAnalysis, TrajectoryBundle
from oddish.evals.analyzer.bucketing import BUCKET_OF
from oddish.evals.analyzer.schemas import Finding

_PROMPT_DIR = Path(__file__).parent / "prompts"
MAP_PROMPT_TEMPLATE = (_PROMPT_DIR / "map.txt").read_text()
REDUCE_PROMPT_TEMPLATE = (_PROMPT_DIR / "reduce.txt").read_text()


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
        counts_block=counts_block, findings_block=findings_block
    )

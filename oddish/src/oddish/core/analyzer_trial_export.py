"""Pure serialization of per-trial analyzer outputs for the S3 trial-dump.

Kept DB/S3-free so the merge logic is unit-testable. Consumed by the analyzer
worker handler when ``AnalyzerModel.save_trial_analyses`` is set.
"""

from __future__ import annotations

from dataclasses import asdict

from oddish.evals.analyzer.schemas import Finding
from oddish.evals.primitives import SubAnalysis


def build_trial_analyses_payload(
    *,
    analyzer_id: str,
    findings: list[Finding],
    subanalyses: list[SubAnalysis],
    counts: dict[str, int],
) -> dict:
    """Merge findings and subanalyses into one per-job JSON-able payload.

    Union of trial ids across both inputs; a trial present in only one appears
    with the other side ``None``. ``trials`` is sorted by ``trial_id`` for
    deterministic output.
    """
    findings_by_trial = {f.trial_id: f for f in findings}
    subs_by_trial = {s.trial_id: s for s in subanalyses}
    trial_ids = sorted(set(findings_by_trial) | set(subs_by_trial))

    trials = []
    for tid in trial_ids:
        finding = findings_by_trial.get(tid)
        finding_dict = None
        if finding is not None:
            finding_dict = asdict(finding)
            finding_dict.pop("trial_id", None)  # redundant with the record key
        sub = subs_by_trial.get(tid)
        trials.append(
            {
                "trial_id": tid,
                "finding": finding_dict,
                "subanalysis": asdict(sub) if sub is not None else None,
            }
        )

    return {"analyzer_id": analyzer_id, "counts": dict(counts), "trials": trials}

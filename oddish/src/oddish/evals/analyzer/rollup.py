"""Derive the three per-model / per-task lanes from persisted findings.

Pure: dicts in, dicts out. No DB, no S3, no LLM, no env — so the whole
aggregation is testable with literals.

Lanes are derived from the classifier's own ``classification``/``subtype``, never
from a finding's ``bucket``/``subcategory``: those are LLM-assigned while the
analyzer's ``breakdown`` counts are host-derived, so the two can disagree and a
hackable task could be laundered into the capability-headroom lane.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from oddish.config import to_anthropic_api_model_id
from oddish.evals.analyzer.bucketing import BUCKET_OF, SUBCATEGORY_OF

_UNKNOWN_MODEL = "unknown"


def _model_key(raw: str | None) -> str:
    """Collapse the spellings of one model. The same model appears plain,
    provider-prefixed, or as a Bedrock inference-profile id."""
    return to_anthropic_api_model_id(raw) or _UNKNOWN_MODEL


def _lane_of(finding: dict) -> str | None:
    """Which lane a finding belongs to, or None if it isn't a failure."""
    bucket = BUCKET_OF.get(finding.get("classification") or "", "other")
    if bucket == "good":
        return "good_failures"
    if bucket == "bad":
        code = SUBCATEGORY_OF.get(finding.get("subtype") or "", "emergent")
        if code == "1a":
            return "task_construction"
        if code == "1b":
            return "reward_hacking"
    return None


def _example(finding: dict) -> dict:
    return {
        "trial_id": finding.get("trial_id", ""),
        "trajectory_link": finding.get("trajectory_link", ""),
        "step_ids": list(finding.get("step_ids") or []),
        "root_cause": finding.get("root_cause", ""),
        "evidence_quote": finding.get("evidence_quote", ""),
    }


def _gaps(findings: list[dict]) -> list[dict]:
    """Group a lane's findings by subtype -> one row per distinct gap."""
    by_gap: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        by_gap[f.get("subtype") or "unknown"].append(f)

    gaps = [
        {
            "gap": gap,
            "subcategory": SUBCATEGORY_OF.get(gap, "emergent"),
            "count": len(items),
            "examples": [_example(f) for f in items],
        }
        for gap, items in by_gap.items()
    ]
    gaps.sort(key=lambda g: (-g["count"], g["gap"]))
    return gaps


def _by_model(findings: list[dict]) -> dict[str, Any]:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        by_model[_model_key(f.get("model"))].append(f)

    groups = [
        {"key": key, "gaps": _gaps(items)} for key, items in by_model.items()
    ]
    groups.sort(key=lambda g: (-sum(x["count"] for x in g["gaps"]), g["key"]))
    return {"axis": "model", "groups": groups}


def _by_task(
    findings: list[dict], models_by_task: dict[str, list[str]] | None
) -> dict[str, Any]:
    by_task: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        by_task[f.get("task_id") or "unknown"].append(f)

    groups = []
    for task_id, items in by_task.items():
        # Both sides normalized, or models_hit could contain a key absent from the
        # roster and the ratio would exceed 1.
        hit = sorted({_model_key(f.get("model")) for f in items})
        if models_by_task is None:
            # No roster persisted (analyzer predates analyzers_006), so the
            # denominator is unknown -- not zero. 0 would read as "no model ran
            # this task", inverting the one number this lane exists to show.
            total = None
        else:
            total = len({_model_key(m) for m in models_by_task.get(task_id, [])})
        groups.append(
            {
                "key": items[0].get("task_path") or task_id,
                "task_id": task_id,
                "models_hit": hit,
                "models_total": total,
                "gaps": _gaps(items),
            }
        )
    groups.sort(key=lambda g: (-sum(x["count"] for x in g["gaps"]), g["key"]))
    return {"axis": "task", "groups": groups}


def build_rollup(
    findings: list[dict], models_by_task: dict[str, list[str]] | None
) -> dict[str, Any]:
    """``models_by_task=None`` means no roster was persisted; task_construction
    then reports ``models_total: None`` (unknown) rather than 0."""
    lanes: dict[str, list[dict]] = {
        "good_failures": [], "task_construction": [], "reward_hacking": []
    }
    for f in findings:
        lane = _lane_of(f)
        if lane is not None:
            lanes[lane].append(f)

    return {
        "good_failures": _by_model(lanes["good_failures"]),
        "task_construction": _by_task(lanes["task_construction"], models_by_task),
        "reward_hacking": _by_model(lanes["reward_hacking"]),
    }

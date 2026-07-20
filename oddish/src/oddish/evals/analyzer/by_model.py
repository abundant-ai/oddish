"""Per-model grouping for report insights. Pure: literals in, dicts out.

Findings record only failures, so per-model finding counts are meaningless
without a denominator drawn from the trials themselves (which include passes).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from oddish.evals.analyzer.bucketing import BUCKET_OF
from oddish.evals.analyzer.rollup import _model_key
from oddish.evals.analyzer.schemas import Finding


def build_denominators(
    trials: list[tuple[str | None, float | None]], findings: list[Finding]
) -> dict[str, dict]:
    """model key -> trial/finding counts.

    ``trials`` is (raw model, reward) per gathered trial. ``reward`` is nullable
    and supports partial credit, so ``scored`` (reward is not None) is the only
    honest denominator for ``solved`` and ``mean_reward``: a null reward means no
    test result was produced, which is not a failure.
    """
    acc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"trials": 0, "scored": 0, "solved": 0, "reward_sum": 0.0,
                 "analyzed": 0, "bad": 0, "good": 0}
    )

    for raw_model, reward in trials:
        d = acc[_model_key(raw_model)]
        d["trials"] += 1
        if reward is not None:
            d["scored"] += 1
            d["reward_sum"] += float(reward)
            if float(reward) >= 1.0:
                d["solved"] += 1

    for f in findings:
        d = acc[_model_key(f.model)]
        d["analyzed"] += 1
        bucket = BUCKET_OF.get(f.classification or "", "other")
        if bucket in ("bad", "good"):
            d[bucket] += 1

    return {
        key: {
            "trials": d["trials"],
            "scored": d["scored"],
            "solved": d["solved"],
            "mean_reward": (d["reward_sum"] / d["scored"]) if d["scored"] else None,
            "analyzed": d["analyzed"],
            "bad": d["bad"],
            "good": d["good"],
        }
        for key, d in acc.items()
    }


BY_MODEL_VERSION = 1


def normalize_entries(
    entries: list[dict], denominators: dict[str, dict], *, bucket: str
) -> list[dict]:
    """Host-authoritative cleanup of the LLM's per-model entries.

    The model name is the LLM's only structural input here, so it is normalized
    and then checked against the denominators — which are derived from trial
    rows. An entry naming a model that never ran is a hallucinated comparison
    group and is dropped, never stored. Mirrors core.py's rule that
    model/task/link on a Finding are host facts, never the model's echo.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = _model_key(entry.get("model"))
        if key not in denominators:
            continue
        if (key, bucket) in seen:
            continue
        seen.add((key, bucket))

        raw_failures = entry.get("distinctive_failures")
        failures = (
            [str(x) for x in raw_failures] if isinstance(raw_failures, list) else []
        )

        out.append({
            "model": key,
            "bucket": bucket,
            "narrative": str(entry.get("narrative") or ""),
            "relative_strengths": str(entry.get("relative_strengths") or ""),
            "relative_weaknesses": str(entry.get("relative_weaknesses") or ""),
            "distinctive_failures": failures,
        })

    out.sort(key=lambda e: e["model"])
    return out


def build_by_model_payload(
    entries: list[dict], comparison: str, denominators: dict[str, dict]
) -> dict:
    return {
        "version": BY_MODEL_VERSION,
        "comparison": comparison,
        "denominators": denominators,
        "models": entries,
    }

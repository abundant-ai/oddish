"""Pure analyzer-eval pipeline: roster -> cohort-aware map -> reduce.

No DB / S3 / auth / env / wall-clock / randomness. The LLM client is injected
via the LLMClient protocol so the pipeline is fully unit-testable and can run
identically hosted (worker) or sandboxed (future runner).
"""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from oddish.evals.primitives import SubAnalysis, TrajectoryBundle
from oddish.evals.analyzer.bucketing import BUCKET_OF, bucket_subanalyses
from oddish.evals.analyzer.prompt_builder import build_map_prompt, build_reduce_prompt
from oddish.evals.analyzer.schemas import (
    Finding,
    AnalyzerEvalConfig,
    AnalyzerEvalInputs,
    AnalyzerEvalOutput,
)

_EMPTY_SECTIONS = {"bad": "", "good": "", "capabilities": "", "headroom": ""}


class LLMClient(Protocol):
    async def complete(
        self, prompt: str, *, model: str, temperature: float, max_tokens: int
    ) -> str: ...


def parse_json(text: str) -> dict:
    """Tolerant JSON extraction: strip code fences, take the outermost object."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model output: {text[:200]!r}")
    return json.loads(t[start : end + 1])


def _default_client() -> LLMClient:
    from anthropic import AsyncAnthropic

    inner = AsyncAnthropic()

    class _Wrap:
        async def complete(self, prompt, *, model, temperature, max_tokens):
            resp = await inner.messages.create(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )

    return _Wrap()


def _roster(bad: list[SubAnalysis], good: list[SubAnalysis]) -> list[dict]:
    rows = []
    for sa in bad:
        rows.append({"trial_id": sa.trial_id, "bucket": "bad",
                     "subtype": sa.subtype, "trajectory_link": sa.trajectory_link})
    for sa in good:
        rows.append({"trial_id": sa.trial_id, "bucket": "good",
                     "subtype": sa.subtype, "trajectory_link": sa.trajectory_link})
    return rows


async def _map_one(
    client: LLMClient, config: AnalyzerEvalConfig,
    bundle: TrajectoryBundle, sa: SubAnalysis, roster: list[dict],
    sem: asyncio.Semaphore,
) -> Finding | None:
    async with sem:
        prompt = build_map_prompt(bundle, sa, roster)
        raw = await client.complete(
            prompt, model=config.analysis_model,
            temperature=config.temperature, max_tokens=config.token_budget,
        )
    try:
        d = parse_json(raw)
    except ValueError:
        return None
    return Finding(
        trial_id=d.get("trial_id", sa.trial_id),
        bucket=d.get("bucket", BUCKET_OF.get(sa.classification, "other")),
        subcategory=d.get("subcategory", "emergent"),
        evidence_quote=d.get("evidence_quote", ""),
        step_indices=list(d.get("step_indices") or []),
        root_cause=d.get("root_cause", ""),
        headroom_signal=d.get("headroom_signal", ""),
        # Trust the host-built link on the bundle, never the model's echo.
        trajectory_link=bundle.trajectory_link,
    )


async def run_analyzer_eval(
    inputs: AnalyzerEvalInputs,
    config: AnalyzerEvalConfig,
    *,
    client: LLMClient | None = None,
) -> AnalyzerEvalOutput:
    bad, good, breakdown = bucket_subanalyses(inputs.subanalyses)
    counts = {"trials": len(inputs.bundles), "bad": len(bad), "good": len(good)}

    if not bad and not good:
        return AnalyzerEvalOutput(
            sections=dict(_EMPTY_SECTIONS), findings=[], counts=counts, breakdown=breakdown
        )

    client = client or _default_client()

    roster = _roster(bad, good)
    by_trial = {b.trial_id: b for b in inputs.bundles}
    sem = asyncio.Semaphore(config.map_concurrency)

    tasks = [
        _map_one(client, config, by_trial[sa.trial_id], sa, roster, sem)
        for sa in (bad + good)
        if sa.trial_id in by_trial
    ]
    findings = [f for f in await asyncio.gather(*tasks) if f is not None]

    reduce_prompt = build_reduce_prompt(findings, counts)
    raw = await client.complete(
        reduce_prompt, model=config.analysis_model,
        temperature=config.temperature, max_tokens=config.token_budget,
    )
    sec = parse_json(raw)
    sections = {
        "bad": sec.get("bad_failure_content", ""),
        "good": sec.get("good_failure_content", ""),
        "capabilities": sec.get("universal_capabilities_content", ""),
        "headroom": sec.get("headroom_analysis", ""),
    }
    return AnalyzerEvalOutput(
        sections=sections, findings=findings, counts=counts, breakdown=breakdown
    )

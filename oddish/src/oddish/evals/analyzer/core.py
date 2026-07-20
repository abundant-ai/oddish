"""Pure analyzer-eval pipeline: roster -> cohort-aware map -> reduce.

No DB / S3 / auth / env / wall-clock / randomness. The LLM client is injected
via the LLMClient protocol so the pipeline is fully unit-testable and can run
identically hosted (worker) or sandboxed (future runner).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from oddish.evals.primitives import SubAnalysis, TrajectoryBundle
from oddish.evals.analyzer.bucketing import BUCKET_OF, bucket_subanalyses
from oddish.evals.analyzer.by_model import build_by_model_payload, normalize_entries
from oddish.evals.analyzer.prompt_builder import build_map_prompt, build_reduce_prompt
from oddish.evals.analyzer.schemas import (
    Finding,
    AnalyzerEvalConfig,
    AnalyzerEvalInputs,
    AnalyzerEvalOutput,
)

logger = logging.getLogger(__name__)

_EMPTY_SECTIONS = {"bad": "", "good": "", "capabilities": "", "headroom": ""}


def _ctx(**kw) -> str:
    """Render step context for log/error messages (small, low-cardinality only)."""
    return ", ".join(f"{k}={v!r}" for k, v in kw.items())


class AnalyzerEvalStepError(RuntimeError):
    """A fatal failure in a named step of run_analyzer_eval.

    Carries the step name so the worker's persisted ``error`` (and any log
    reader) can see which stage broke and on what input, instead of a bare
    exception. Non-fatal per-trial map failures do NOT raise this — they are
    logged and skipped so one bad trajectory can't sink the whole eval.
    """

    def __init__(self, step: str, context: str, cause: BaseException):
        self.step = step
        self.context = context
        super().__init__(f"analyzer-eval step {step!r} failed ({context}): {cause}")


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


def build_roster(bad: list[SubAnalysis], good: list[SubAnalysis]) -> list[dict]:
    rows = []
    for sa in bad:
        rows.append({"trial_id": sa.trial_id, "bucket": "bad",
                     "subtype": sa.subtype, "trajectory_link": sa.trajectory_link})
    for sa in good:
        rows.append({"trial_id": sa.trial_id, "bucket": "good",
                     "subtype": sa.subtype, "trajectory_link": sa.trajectory_link})
    return rows


def _models_by_task_from_bundles(bundles: list[TrajectoryBundle]) -> dict[str, list[str]]:
    """task_id -> distinct models that ran it, including trials that PASSED.

    Every trial gets a bundle (failures in full, the rest as stubs) and bundles
    carry model, so this needs nothing the pure core doesn't already have.
    analyzer_handler._models_by_task derives the same thing from rows for
    persistence, and must keep doing so for eval strategies that never build
    AnalyzerEvalInputs. Same semantics, different input; keep them in step.
    """
    by_task: dict[str, set[str]] = {}
    for b in bundles:
        if b.model:
            by_task.setdefault(b.task_id, set()).add(b.model)
    return {k: sorted(v) for k, v in by_task.items()}


async def _map_one(
    client: LLMClient, config: AnalyzerEvalConfig,
    bundle: TrajectoryBundle, sa: SubAnalysis, roster: list[dict],
    sem: asyncio.Semaphore,
) -> Finding | None:
    p = f"[{config.job_kind}] "
    bucket = BUCKET_OF.get(sa.classification, "other")
    try:
        async with sem:
            prompt = build_map_prompt(bundle, sa, roster)
            raw = await client.complete(
                prompt, model=config.analysis_model,
                temperature=config.temperature, max_tokens=config.token_budget,
            )
    except Exception as exc:  # noqa: BLE001 — one trial must not sink the eval
        logger.warning(
            p + "analyzer-eval map: LLM call failed for trial %s (%s); skipping. %s",
            sa.trial_id,
            _ctx(bucket=bucket, subtype=sa.subtype, model=config.analysis_model),
            exc,
        )
        return None
    try:
        d = parse_json(raw)
    except ValueError as exc:
        logger.warning(
            p + "analyzer-eval map: unparseable model output for trial %s (%s); "
            "skipping. reason=%s; output_head=%.200r",
            sa.trial_id,
            _ctx(bucket=bucket, subtype=sa.subtype, output_chars=len(raw)),
            exc,
            raw,
        )
        return None
    return Finding(
        trial_id=d.get("trial_id", sa.trial_id),
        bucket=d.get("bucket", BUCKET_OF.get(sa.classification, "other")),
        subcategory=d.get("subcategory", "emergent"),
        evidence_quote=d.get("evidence_quote", ""),
        step_ids=list(d.get("step_ids") or []),
        root_cause=d.get("root_cause", ""),
        headroom_signal=d.get("headroom_signal", ""),
        # Host facts, never the model's echo.
        trajectory_link=bundle.trajectory_link,
        model=sa.model,
        classification=sa.classification,
        subtype=sa.subtype,
        task_id=bundle.task_id,
        task_path=bundle.task_path,
    )


async def run_analyzer_eval(
    inputs: AnalyzerEvalInputs,
    config: AnalyzerEvalConfig,
    *,
    client: LLMClient | None = None,
    denominators: dict[str, dict] | None = None,
) -> AnalyzerEvalOutput:
    p = f"[{config.job_kind}] "
    logger.info(
        p + "analyzer-eval starting: %s",
        _ctx(bundles=len(inputs.bundles), subanalyses=len(inputs.subanalyses),
             model=config.analysis_model, map_concurrency=config.map_concurrency),
    )

    # Step 1 — bucket the per-trial subanalyses into bad / good cohorts.
    try:
        bad, good, breakdown = bucket_subanalyses(inputs.subanalyses)
    except Exception as exc:
        ctx = _ctx(subanalyses=len(inputs.subanalyses))
        logger.exception(p + "analyzer-eval bucketing failed (%s)", ctx)
        raise AnalyzerEvalStepError("bucketing", ctx, exc) from exc

    counts = {"trials": len(inputs.bundles), "bad": len(bad), "good": len(good)}
    logger.info(p + "analyzer-eval bucketed: %s", _ctx(**counts, breakdown=breakdown))

    # Zero-work fast path: no failures to analyze. Must return BEFORE building a
    # client so the pipeline stays pure / needs no API key when there's nothing
    # to do.
    if not bad and not good:
        return AnalyzerEvalOutput(
            sections=dict(_EMPTY_SECTIONS), findings=[], counts=counts, breakdown=breakdown,
            subanalyses=inputs.subanalyses,
        )

    # Step 2 — obtain the LLM client (constructs the default Anthropic client,
    # which reads ANTHROPIC_API_KEY, unless one was injected).
    try:
        client = client or _default_client()
    except Exception as exc:
        ctx = _ctx(injected=False, model=config.analysis_model)
        logger.exception(p + "analyzer-eval client init failed (%s)", ctx)
        raise AnalyzerEvalStepError("client-init", ctx, exc) from exc

    # Step 3 — build the shared roster + the trial-id -> bundle index, and pair
    # each failure subanalysis with its trajectory bundle. Subanalyses with no
    # matching bundle can't be mapped; log and skip them rather than KeyError.
    try:
        roster = build_roster(bad, good)
        by_trial = {b.trial_id: b for b in inputs.bundles}
        selected = [sa for sa in (bad + good) if sa.trial_id in by_trial]
        missing = [sa.trial_id for sa in (bad + good) if sa.trial_id not in by_trial]
    except Exception as exc:
        ctx = _ctx(bad=len(bad), good=len(good), bundles=len(inputs.bundles))
        logger.exception(p + "analyzer-eval roster/index build failed (%s)", ctx)
        raise AnalyzerEvalStepError("roster", ctx, exc) from exc

    if missing:
        logger.warning(
            p + "analyzer-eval: %d failure subanalyses have no matching trajectory "
            "bundle and will be skipped: %s", len(missing), missing,
        )

    # Step 4 — fan out one map call per failure trial. Individual failures are
    # non-fatal (logged + skipped inside _map_one); return_exceptions is a
    # backstop for anything that slips past it.
    logger.info(p + "analyzer-eval map: fanning out over %d trials", len(selected))
    sem = asyncio.Semaphore(config.map_concurrency)
    results = await asyncio.gather(
        *[_map_one(client, config, by_trial[sa.trial_id], sa, roster, sem)
          for sa in selected],
        return_exceptions=True,
    )
    findings = []
    for sa, res in zip(selected, results):
        if isinstance(res, BaseException):
            logger.warning(
                p + "analyzer-eval map: unexpected error for trial %s (%s); skipping. %s",
                sa.trial_id, _ctx(subtype=sa.subtype), res,
            )
        elif res is not None:
            findings.append(res)
    logger.info(
        p + "analyzer-eval map done: %d findings from %d trials (%d skipped)",
        len(findings), len(selected), len(selected) - len(findings),
    )

    # We're past the zero-work early return, so the cohort was non-empty. If it
    # still produced no findings — every map failed/was unparseable, or every
    # subanalysis was dropped for a missing bundle — reducing would yield blank
    # sections that still look like a completed analysis. Treat it as fatal
    # (parallels qa_handler refusing to synthesize a verdict from zero
    # classifications).
    if not findings:
        ctx = _ctx(
            cohort=len(bad) + len(good), attempted=len(selected),
            missing_bundles=len(missing), findings=0,
        )
        logger.error(
            p + "analyzer-eval map: no findings from a non-empty cohort; "
            "nothing to reduce (%s)", ctx,
        )
        raise AnalyzerEvalStepError(
            "map", ctx,
            RuntimeError("no findings produced from a non-empty failure cohort"),
        )

    # Step 5 — reduce the per-trial findings into the four narrative sections.
    reduce_prompt = build_reduce_prompt(
        findings, counts, _models_by_task_from_bundles(inputs.bundles),
        denominators=denominators,
    )
    try:
        raw = await client.complete(
            reduce_prompt, model=config.analysis_model,
            temperature=config.temperature, max_tokens=config.token_budget,
        )
    except Exception as exc:
        ctx = _ctx(findings=len(findings), counts=counts, model=config.analysis_model)
        logger.exception(p + "analyzer-eval reduce LLM call failed (%s)", ctx)
        raise AnalyzerEvalStepError("reduce-llm", ctx, exc) from exc

    try:
        sec = parse_json(raw)
    except ValueError as exc:
        ctx = _ctx(findings=len(findings), output_chars=len(raw))
        logger.error(
            p + "analyzer-eval reduce: unparseable model output (%s); output_head=%.500r",
            ctx, raw,
        )
        raise AnalyzerEvalStepError("reduce-parse", ctx, exc) from exc

    sections = {
        "bad": sec.get("bad_failure_content", ""),
        "good": sec.get("good_failure_content", ""),
        "capabilities": sec.get("universal_capabilities_content", ""),
        "headroom": sec.get("headroom_analysis", ""),
    }

    # by_model is additive: a reduce that omits it (older prompt, truncated
    # output) still yields a valid report, it just renders in the legacy shape.
    by_model = None
    if denominators:
        raw_entries = sec.get("by_model")
        entries = normalize_entries(
            raw_entries if isinstance(raw_entries, list) else [],
            denominators,
            bucket="all",
        )
        by_model = build_by_model_payload(
            entries, str(sec.get("cross_model_comparison") or ""), denominators
        )
        logger.info(
            p + "analyzer-eval reduce: %d/%d models described",
            len(entries), len(denominators),
        )

    logger.info(p + "analyzer-eval complete: %d findings, sections rendered", len(findings))
    return AnalyzerEvalOutput(
        sections=sections, findings=findings, counts=counts, breakdown=breakdown,
        reduce_prompt=reduce_prompt, subanalyses=inputs.subanalyses,
        by_model=by_model,
    )

"""Run a bucket as parallel MAP AnalyzerBlocks followed by one REDUCE block."""

from __future__ import annotations

import asyncio
import logging

from api.services.blocks.analyzer.analyzer_parse import parse_cohort_result
from api.services.blocks.analyzer.analyzer_prompt import (
    CLI_DEST,
    OUT_DIR,
    REDUCE_PATH,
    build_map_batch_prompt,
    build_reduce_only_prompt,
    build_system_prompt,
    findings_path,
)
from api.services.sandbox.stream_render import render_event
from oddish.config import settings
from oddish.evals.analyzer.schemas import Finding
from oddish.evals.primitives import SubAnalysis

logger = logging.getLogger(__name__)


def _render_stream(raw_chunks: list[str]) -> str:
    """Turn a block's accumulated ``json.dumps(event)`` chunks back into the
    rendered lines parse_cohort_result scans for MAP/REDUCE markers, so the
    stream fallback survives the move to blocks."""
    import json as _json

    lines: list[str] = []
    for chunk in raw_chunks:
        try:
            line = render_event(_json.loads(chunk))
        except Exception:  # noqa: BLE001 — a malformed chunk just can't render
            continue
        if line:
            lines.append(line)
    return "\n".join(lines)


# stream_chat has no --model flag, so force the model via env; Claude Code
# honors ANTHROPIC_MODEL and the system/init event echoes it back.
HAIKU_MODEL = "claude-haiku-4-5-20251001"
# Now bounds the whole batched map + reduce sequence, not a single agent turn:
# a 97-trial cohort is ~10 map calls plus a reduce, so the old 30 min would trip
# on a cohort that is merely large rather than wedged.
COHORT_TIMEOUT_SECONDS = 5400  # 90 min


# Trials per MAP agent. Sized against the haiku window: each trial costs the
# CLI's trajectory cap (~8KB ≈ 2k tokens) plus narration and the emitted
# finding, and every batch re-pays the roster block (both cohorts). 10 keeps a
# batch near ~30k tokens -- well inside 200k, with room for trajectories that
# blow past the cap's expectation.
#
# It is deliberately conservative: overshooting resurrects the original bug
# silently and only on large cohorts, while undershooting costs a few extra
# sequential calls in a job that already takes minutes.
MAP_BATCH_SIZE = 10

# Trajectory bytes the in-sandbox CLI returns per `--trajectory` fetch (tail
# only -- failures land at the end). Exported to the sandbox as
# ODDISH_QUERY_TRAJ_TAIL_BYTES so this is the ONE place the budget is set: the
# CLI reads it from env, and build_system_prompt quotes the same number when it
# tells the agent how to widen. A literal in either place could drift from the
# other and the prompt would advertise a budget the CLI does not honour.
TRAJ_TAIL_BYTES = 8000


def batches(cohort: list[SubAnalysis]) -> list[list[SubAnalysis]]:
    """Split a cohort into per-agent MAP batches.

    WHY THIS EXISTS: the old cohort runner handed one claude process the ENTIRE
    cohort. Context grows linearly with trials, so a 97-trial cohort needed
    ~194k tokens -- the whole haiku window. The agent burned its context during
    MAP and never reached REDUCE, so reduce.json was never written and the
    cohort died on a 0B parse. An 8-trial cohort fit, which is why 'bad' passed
    and 'good' failed on the same run.

    Each batch runs in a FRESH claude process (stream_chat spawns a new one and
    never passes --resume), so context resets per batch; findings.jsonl on disk
    carries results across. Peak context is O(batch) instead of O(cohort).
    """
    if len(cohort) <= MAP_BATCH_SIZE:
        # A small cohort is one MAP batch, but NOT the pre-batching shape: that
        # ran MAP and REDUCE in a single context, where reduce already held the
        # findings. Reduce is now always its own process reading them back off
        # disk, so even an 8-trial cohort that passed before now depends on the
        # map agent writing parseable lines. parse_cohort_result's stream
        # fallback recovers the findings if it does not; the reduce sections are
        # what is genuinely at risk.
        return [cohort]
    return [
        cohort[i : i + MAP_BATCH_SIZE] for i in range(0, len(cohort), MAP_BATCH_SIZE)
    ]


async def run_analyzer_blocks(
    *,
    bucket: str,
    cohort: list[SubAnalysis],
    roster: list[dict],
    counts: dict,
    oracle_by_trial: dict[str, str],
    host_by_trial: dict[str, dict],
    analyzer_id: str,
    anthropic_key: str,
    api_base: str,
    api_key: str,
    cli_src: bytes,
    parallelism: int,
    models_by_task: dict[str, list[str]] | None = None,
    denominators: dict[str, dict] | None = None,
) -> tuple[list[Finding], dict[str, str], tuple[list[dict], str]]:
    from oddish.blocks.analyzer.analyzer_block import (
        AnalyzerBlock,
        AnalyzerInput,
        AnalyzerType,
    )
    from oddish.blocks.analyzer.analyzer_llm_client import (
        LLMClientType,
        SandboxConfig,
    )

    tag = f"[analyzer {analyzer_id}][{bucket}]"
    plan = batches(cohort)
    raw_stream: list[str] = []
    reduce_b = b""
    findings_b = b""

    def sandbox_config(
        *,
        phase: str,
        files_to_upload: dict[str, bytes] | None = None,
    ) -> SandboxConfig:
        return SandboxConfig(
            oddish_api_base_url=api_base,
            oddish_api_key=api_key,
            trajectory_tail_bytes=TRAJ_TAIL_BYTES,
            session_id=f"analyzer-{phase}",
            labels={
                "purpose": "analyzer-block",
                "analyzer": analyzer_id,
                "bucket": bucket,
                "phase": phase,
            },
            files_to_upload={
                CLI_DEST: cli_src,
                **(files_to_upload or {}),
            },
            setup_commands=(f"mkdir -p {OUT_DIR}",),
            auto_stop_minutes=settings.daytona_auto_stop_interval_mins,
            auto_delete_minutes=settings.daytona_auto_delete_interval_mins,
            snapshot=settings.analyzer_snapshot or None,
        )

    semaphore = asyncio.Semaphore(max(1, parallelism))

    async def run_map(i: int, batch: list[SubAnalysis]):
        async with semaphore:
            logger.info("%s map block %d/%d (%d trials)", tag, i, len(plan), len(batch))
            block = AnalyzerBlock(
                analyzer_type=AnalyzerType.TRAJECTORY_FAILURE_ANALYSIS,
                llm_client_type=LLMClientType.SANDBOX,
                input=AnalyzerInput(
                    input={
                        "bucket": bucket,
                        "batch_no": i,
                        "trials": [s.trial_id for s in batch],
                    },
                    files_to_download=[findings_path(i)],
                ),
                prompt=build_map_batch_prompt(
                    bucket,
                    batch,
                    roster,
                    oracle_by_trial,
                    i,
                    len(plan),
                    TRAJ_TAIL_BYTES,
                ),
                system_prompt=build_system_prompt(TRAJ_TAIL_BYTES),
                model=HAIKU_MODEL,
                api_key=anthropic_key,
                analyzer_id=analyzer_id,
                sandbox_config=sandbox_config(phase=f"map-{i}"),
            )
            output = await block.run()
            return i, output.output.get(findings_path(i), ""), block._chunks

    try:
        async with asyncio.timeout(COHORT_TIMEOUT_SECONDS):
            map_results = await asyncio.gather(
                *(run_map(i, batch) for i, batch in enumerate(plan, start=1))
            )
            uploaded_findings: dict[str, bytes] = {}
            parts: list[str] = []
            for i, text, chunks in sorted(map_results):
                raw_stream.extend(chunks)
                if not text.strip():
                    logger.warning("%s map block %d wrote no findings", tag, i)
                    continue
                normalized = text if text.endswith("\n") else text + "\n"
                parts.append(normalized)
                uploaded_findings[findings_path(i)] = normalized.encode()
            findings_b = "".join(parts).encode()

            logger.info("%s reduce over %d batches", tag, len(plan))
            reduce_block = AnalyzerBlock(
                analyzer_type=AnalyzerType.TRAJECTORY_FAILURE_ANALYSIS,
                llm_client_type=LLMClientType.SANDBOX,
                input=AnalyzerInput(
                    input={"bucket": bucket, "phase": "reduce"},
                    files_to_download=[REDUCE_PATH],
                ),
                prompt=build_reduce_only_prompt(
                    bucket, counts, len(plan), models_by_task, denominators
                ),
                system_prompt=None,
                model=HAIKU_MODEL,
                api_key=anthropic_key,
                analyzer_id=analyzer_id,
                sandbox_config=sandbox_config(
                    phase="reduce",
                    files_to_upload=uploaded_findings,
                ),
            )
            reduce_out = await reduce_block.run()
            raw_stream.extend(reduce_block._chunks)

            reduce_b = reduce_out.output.get(REDUCE_PATH, "").encode()
    except TimeoutError as exc:
        raise RuntimeError(
            f"analyzer block run {bucket!r} exceeded {COHORT_TIMEOUT_SECONDS}s"
        ) from exc

    findings, sections, by_model = parse_cohort_result(
        bucket,
        reduce_b,
        findings_b,
        _render_stream(raw_stream),
        # Scoped to this cohort, so a finding for someone else's trial is dropped.
        {sa.trial_id: host_by_trial[sa.trial_id] for sa in cohort},
    )
    logger.info(
        "%s done: %d findings, sections=%s", tag, len(findings), sorted(sections)
    )
    return findings, sections, by_model

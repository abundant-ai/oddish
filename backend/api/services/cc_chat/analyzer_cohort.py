"""Run one cohort's MAP -> REDUCE inside a Daytona sandbox.

One runner, parameterized by bucket. The agent pulls trajectories itself via the
oddish-query CLI, so nothing here reads S3.
"""

from __future__ import annotations

import asyncio
import logging

from api.services.cc_chat.analyzer_parse import parse_cohort_result
from api.services.cc_chat.analyzer_prompt import (
    CLI_DEST,
    OUT_DIR,
    REDUCE_PATH,
    build_map_batch_prompt,
    build_reduce_only_prompt,
    build_system_prompt,
    findings_path,
)
from api.services.cc_chat.stream_render import render_event
from oddish.config import settings
from oddish.evals.analyzer.schemas import CapabilityProposal, Finding
from oddish.evals.analyzer.taxonomy import Taxonomy
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

    WHY THIS EXISTS: run_cohort used to hand one claude process the ENTIRE
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


async def run_cohort(
    client,
    runtime,
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
    taxonomy: Taxonomy,
    models_by_task: dict[str, list[str]] | None = None,
    denominators: dict[str, dict] | None = None,
) -> tuple[list[Finding], dict[str, str], tuple[list[dict], str], list[CapabilityProposal]]:
    # Imported here so the module still loads if the block layer is refactored;
    # the cohort is the only caller that wires blocks to a shared sandbox client.
    from api.services.blocks.analyzer.analyzer_block import (
        AnalyzerBlock,
        AnalyzerInput,
        AnalyzerType,
    )
    from api.services.blocks.analyzer.analyzer_llm_client import (
        LLMClientType,
        SandboxAnalyzerLLMClient,
    )

    tag = f"[analyzer {analyzer_id}][{bucket}]"
    # map_rubric.txt unconditionally follows the capabilities rubric with "if
    # none of the above fit, author a new one", so an empty taxonomy would get
    # every good-bucket finding a fabricated capability_slug while the job still
    # reports SUCCESS. Mirrors the same guard in
    # oddish/evals/analyzer/core.py's run_analyzer_eval. Every map batch renders
    # the rubric (build_map_batch_prompt calls map_rubric(taxonomy)
    # unconditionally); the bad bucket is unaffected by an empty one because it
    # classifies task defects (1a/1b), and _finding_from/_merge_proposals gate
    # capability_slug to the good bucket structurally, so a blank rubric can't
    # make it fabricate one. Guarded before the batch loop so a bad taxonomy
    # fails loud instead of after paying for a sandbox.
    if bucket == "good" and cohort and not taxonomy.capabilities:
        raise RuntimeError(
            f"{tag} refusing to run the good bucket against an empty taxonomy "
            "(no capabilities loaded); would fabricate a novel capability for "
            "every finding"
        )
    plan = batches(cohort)
    sandbox = None
    # Accumulated json.dumps(event) chunks across every block, rendered back into
    # marker lines for parse_cohort_result's stream fallback. Never persisted.
    raw_stream: list[str] = []
    reduce_b = b""
    findings_b = b""
    try:
        async with asyncio.timeout(COHORT_TIMEOUT_SECONDS):
            sandbox = await client.create_sandbox(
                env_vars={
                    "ANTHROPIC_API_KEY": anthropic_key,
                    "ANTHROPIC_MODEL": HAIKU_MODEL,
                    "ODDISH_API_BASE_URL": api_base,
                    "ODDISH_API_KEY": api_key,
                    "ODDISH_QUERY_TRAJ_TAIL_BYTES": str(TRAJ_TAIL_BYTES),
                },
                # auto_delete is currently inert: RealDaytonaClient forces
                # ephemeral=True, which zeroes Daytona's auto_delete_interval, so
                # auto_stop is the only backstop that actually fires.
                auto_stop_minutes=settings.daytona_auto_stop_interval_mins,
                auto_delete_minutes=settings.daytona_auto_delete_interval_mins,
                labels={
                    "purpose": "analyzer-cohort",
                    "analyzer": analyzer_id,
                    "bucket": bucket,
                },
            )
            logger.info("%s sandbox id=%s (%d trials)", tag, sandbox.id, len(cohort))
            await client.create_session(sandbox, session_id="cc")
            await runtime.install(client, sandbox)
            await client.exec_sync(sandbox, command=f"mkdir -p {OUT_DIR}")
            await client.upload_file(sandbox, dest_path=CLI_DEST, content=cli_src)

            # One shared client wrapping this cohort's sandbox; the blocks stream
            # through it and never close it (an injected client is the caller's,
            # and the finally below owns the sandbox lifecycle). Each block's
            # stream_chat gets claude_session_id=None -- a fresh process per
            # block is the whole point; resuming would chain contexts and
            # reintroduce the linear context growth batching exists to prevent.
            llm = SandboxAnalyzerLLMClient(
                sandbox=sandbox,
                daytona_client=client,
                runtime=runtime,
                daytona_session_id="cc",
            )

            for i, batch in enumerate(plan, start=1):
                logger.info(
                    "%s map batch %d/%d (%d trials)", tag, i, len(plan), len(batch)
                )
                # The fetch-more system prompt rides EVERY map block: context
                # resets per block, so batch 3 has no memory that batch 1 was
                # told it could widen the tail budget. Map blocks are producers
                # only (they write findings-<i>.jsonl); the reduce block below
                # enumerates every findings_path(i), so nothing to download here.
                map_block = AnalyzerBlock(
                    analyzer_type=AnalyzerType.TRAJECTORY_FAILURE_ANALYSIS,
                    llm_client_type=LLMClientType.SANDBOX,
                    input=AnalyzerInput(
                        input={
                            "bucket": bucket,
                            "batch_no": i,
                            "trials": [s.trial_id for s in batch],
                        },
                    ),
                    prompt=build_map_batch_prompt(
                        bucket,
                        batch,
                        roster,
                        oracle_by_trial,
                        i,
                        len(plan),
                        TRAJ_TAIL_BYTES,
                        taxonomy,
                    ),
                    system_prompt=build_system_prompt(TRAJ_TAIL_BYTES),
                    model=HAIKU_MODEL,
                    analyzer_id=analyzer_id,
                    client=llm,
                )
                await map_block.run()
                raw_stream.extend(map_block._chunks)

            # REDUCE gets NO fetch-more system prompt: its whole job is to read
            # the findings files, and telling it to pull trajectories would both
            # contradict its user prompt and let it refetch the entire cohort --
            # recreating the context blowup this batching exists to prevent.
            # files_to_download makes the block return {path: decoded str} for
            # reduce.json + every batch's findings file (a missing one decodes to
            # "" so one blank batch costs only itself, not the cohort).
            logger.info("%s reduce over %d batches", tag, len(plan))
            reduce_block = AnalyzerBlock(
                analyzer_type=AnalyzerType.TRAJECTORY_FAILURE_ANALYSIS,
                llm_client_type=LLMClientType.SANDBOX,
                input=AnalyzerInput(
                    input={"bucket": bucket, "phase": "reduce"},
                    files_to_download=[
                        REDUCE_PATH,
                        *(findings_path(i) for i in range(1, len(plan) + 1)),
                    ],
                ),
                prompt=build_reduce_only_prompt(
                    bucket, counts, len(plan), models_by_task, denominators
                ),
                system_prompt=None,
                model=HAIKU_MODEL,
                analyzer_id=analyzer_id,
                client=llm,
            )
            reduce_out = await reduce_block.run()
            raw_stream.extend(reduce_block._chunks)

            files = reduce_out.output  # {path: decoded str}
            reduce_b = files.get(REDUCE_PATH, "").encode("utf-8")
            # Concatenate the per-batch files host-side rather than trusting the
            # sandbox to have merged them. A blank batch file costs only its own
            # batch, instead of taking the cohort down.
            parts = []
            for i in range(1, len(plan) + 1):
                text = files.get(findings_path(i), "")
                if not text.strip():
                    logger.warning("%s batch %d wrote no findings", tag, i)
                    continue
                parts.append(text if text.endswith("\n") else text + "\n")
            findings_b = "".join(parts).encode("utf-8")
    except TimeoutError as exc:
        raise RuntimeError(
            f"analyzer cohort {bucket!r} exceeded {COHORT_TIMEOUT_SECONDS}s"
        ) from exc
    finally:
        if sandbox is not None:
            try:
                await client.delete_sandbox(sandbox)
            except Exception as exc:  # noqa: BLE001 — auto_delete is the backstop
                logger.warning("%s sandbox delete failed: %s", tag, exc)

    findings, sections, by_model, proposals = parse_cohort_result(
        bucket, reduce_b, findings_b, _render_stream(raw_stream),
        # Scoped to this cohort, so a finding for someone else's trial is dropped.
        {sa.trial_id: host_by_trial[sa.trial_id] for sa in cohort},
    )
    logger.info(
        "%s done: %d findings, %d proposals, sections=%s",
        tag, len(findings), len(proposals), sorted(sections),
    )
    return findings, sections, by_model, proposals

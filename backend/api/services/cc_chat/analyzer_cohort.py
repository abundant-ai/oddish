"""Run one cohort's MAP -> REDUCE inside a Daytona sandbox.

One runner, parameterized by bucket. The agent pulls trajectories itself via the
oddish-query CLI, so nothing here reads S3.
"""

from __future__ import annotations

import asyncio
import logging

from api.services.cc_chat.analyzer_parse import parse_and_save_cohort_result
from api.services.cc_chat.analyzer_prompt import (
    CLI_DEST,
    FINDINGS_GLOB,
    OUT_DIR,
    REDUCE_PATH,
    build_map_batch_prompt,
    build_reduce_only_prompt,
    build_system_prompt,
    findings_path,
)
from api.services.cc_chat.stream_render import render_event
from oddish.config import settings
from oddish.evals.analyzer.schemas import Finding
from oddish.evals.primitives import SubAnalysis

logger = logging.getLogger(__name__)

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


async def _download(client, sandbox, path: str) -> bytes:
    try:
        return await client.download_file(sandbox, src_path=path)
    except Exception as exc:  # noqa: BLE001 — a missing file is the fallback path
        logger.warning("analyzer-sandbox: could not download %s (%s)", path, exc)
        return b""


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
    models_by_task: dict[str, list[str]] | None = None,
) -> tuple[list[Finding], dict[str, str]]:
    tag = f"[analyzer {analyzer_id}][{bucket}]"
    plan = batches(cohort)
    # Retained only to serve the parse-fallback; never persisted.
    stream_lines: list[str] = []
    sandbox = None
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

            async def _turn(prompt: str, label: str, system_prompt=None) -> None:
                # claude_session_id=None every time: a fresh process with a
                # fresh context is the whole point. Passing --resume here would
                # chain contexts and reintroduce the linear growth.
                async for evt in runtime.stream_chat(
                    client,
                    sandbox,
                    content=prompt,
                    claude_session_id=None,
                    daytona_session_id="cc",
                    system_prompt=system_prompt,
                ):
                    line = render_event(evt)
                    if line:
                        stream_lines.append(line)
                        logger.info("%s[%s] %s", tag, label, line)

            for i, batch in enumerate(plan, start=1):
                logger.info(
                    "%s map batch %d/%d (%d trials)", tag, i, len(plan), len(batch)
                )
                # The fetch-more system prompt goes on MAP turns only, and on
                # every one of them: context resets per batch, so batch 3 has no
                # memory that batch 1 was told it could widen the tail budget.
                await _turn(
                    build_map_batch_prompt(
                        bucket,
                        batch,
                        roster,
                        oracle_by_trial,
                        i,
                        len(plan),
                        TRAJ_TAIL_BYTES,
                    ),
                    f"map {i}/{len(plan)}",
                    system_prompt=build_system_prompt(TRAJ_TAIL_BYTES),
                )

            # REDUCE gets NO fetch-more prompt: its whole job is to read
            # findings.jsonl, and telling it to pull trajectories would both
            # contradict its user prompt and let it refetch the entire cohort --
            # recreating the context blowup this batching exists to prevent.
            logger.info("%s reduce over %s", tag, FINDINGS_GLOB)
            await _turn(
                build_reduce_only_prompt(bucket, counts, len(plan), models_by_task),
                "reduce",
            )

            reduce_b = await _download(client, sandbox, REDUCE_PATH)
            # Concatenate the per-batch files host-side rather than trusting the
            # sandbox to have merged them. A missing batch file yields b"" and
            # costs only its own batch, instead of taking the cohort down.
            parts = []
            for i in range(1, len(plan) + 1):
                b = await _download(client, sandbox, findings_path(i))
                if not b.strip():
                    logger.warning("%s batch %d wrote no findings", tag, i)
                    continue
                parts.append(b if b.endswith(b"\n") else b + b"\n")
            findings_b = b"".join(parts)
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

    findings, sections = await parse_and_save_cohort_result(
        bucket,
        reduce_b,
        findings_b,
        "\n".join(stream_lines),
        # Scoped to this cohort, so a finding for someone else's trial is dropped.
        {sa.trial_id: host_by_trial[sa.trial_id] for sa in cohort},
        analyzer_id,
    )
    logger.info(
        "%s done: %d findings, sections=%s", tag, len(findings), sorted(sections)
    )
    return findings, sections

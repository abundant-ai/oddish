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
    FINDINGS_PATH,
    OUT_DIR,
    REDUCE_PATH,
    build_cohort_prompt,
)
from api.services.cc_chat.stream_render import render_event
from oddish.config import settings
from oddish.evals.analyzer.schemas import CapabilityProposal, Finding
from oddish.evals.analyzer.taxonomy import Taxonomy
from oddish.evals.primitives import SubAnalysis

logger = logging.getLogger(__name__)

# stream_chat has no --model flag, so force the model via env; Claude Code
# honors ANTHROPIC_MODEL and the system/init event echoes it back.
HAIKU_MODEL = "claude-haiku-4-5-20251001"
COHORT_TIMEOUT_SECONDS = 1800  # 30 min; one wedged agent must not hold the job


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
    taxonomy: Taxonomy,
) -> tuple[list[Finding], dict[str, str], list[CapabilityProposal]]:
    tag = f"[analyzer {analyzer_id}][{bucket}]"
    # map_rubric.txt unconditionally follows a blank capabilities rubric with
    # "if none of the above fit, author a new one", so an empty taxonomy would
    # get every good-bucket finding a fabricated capability_slug while the job
    # still reports SUCCESS. Mirrors the same guard in
    # oddish/evals/analyzer/core.py's run_analyzer_eval. The bad bucket DOES
    # render the rubric (build_cohort_prompt calls map_rubric(taxonomy)
    # unconditionally) but is unaffected by an empty one: it classifies task
    # defects (1a/1b), and _finding_from/_merge_proposals gate capability_slug
    # to the good bucket structurally, so a blank rubric can't make it fabricate
    # one.
    if bucket == "good" and cohort and not taxonomy.capabilities:
        raise RuntimeError(
            f"{tag} refusing to run the good bucket against an empty taxonomy "
            "(no capabilities loaded); would fabricate a novel capability for "
            "every finding"
        )
    prompt = build_cohort_prompt(bucket, cohort, roster, counts, oracle_by_trial, taxonomy)
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
                },
                # auto_delete is currently inert: RealDaytonaClient forces
                # ephemeral=True, which zeroes Daytona's auto_delete_interval, so
                # auto_stop is the only backstop that actually fires.
                auto_stop_minutes=settings.daytona_auto_stop_interval_mins,
                auto_delete_minutes=settings.daytona_auto_delete_interval_mins,
                labels={"purpose": "analyzer-cohort", "analyzer": analyzer_id,
                        "bucket": bucket},
            )
            logger.info("%s sandbox id=%s (%d trials)", tag, sandbox.id, len(cohort))
            await client.create_session(sandbox, session_id="cc")
            await runtime.install(client, sandbox)
            await client.exec_sync(sandbox, command=f"mkdir -p {OUT_DIR}")
            await client.upload_file(sandbox, dest_path=CLI_DEST, content=cli_src)

            async for evt in runtime.stream_chat(
                client, sandbox, content=prompt,
                claude_session_id=None, daytona_session_id="cc",
            ):
                line = render_event(evt)
                if line:
                    stream_lines.append(line)
                    logger.info("%s %s", tag, line)

            reduce_b = await _download(client, sandbox, REDUCE_PATH)
            findings_b = await _download(client, sandbox, FINDINGS_PATH)
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

    findings, sections, proposals = parse_cohort_result(
        bucket, reduce_b, findings_b, "\n".join(stream_lines),
        # Scoped to this cohort, so a finding for someone else's trial is dropped.
        {sa.trial_id: host_by_trial[sa.trial_id] for sa in cohort},
    )
    logger.info(
        "%s done: %d findings, %d proposals, sections=%s",
        tag, len(findings), len(proposals), sorted(sections),
    )
    return findings, sections, proposals

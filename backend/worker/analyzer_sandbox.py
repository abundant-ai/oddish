"""Hosted sandbox analyzer: one Daytona cohort agent for bad, one for good.

Registered over the core ANALYZER handler at worker container load. Only the
eval strategy is swapped -- run_analyzer_generation_job's heartbeat, liveness
checks, and shielded persist are reused untouched.

Unlike the core path this never reads S3: the agent pulls each trajectory
itself via the oddish-query CLI, which is the whole point (a trajectory no
longer has to fit a context window).
"""

from __future__ import annotations

import asyncio
import logging
import os
from importlib.resources import files
from typing import Any

from api.services.cc_chat.analyzer_cohort import run_cohort
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.cc_chat.daytona_client import RealDaytonaClient
from oddish.config import settings
from oddish.core.analyzer_inputs import (
    models_by_task_from_rows,
    subanalysis_from_trial,
    trial_model_rewards,
)
from oddish.evals.analyzer.bucketing import bucket_subanalyses
from oddish.evals.analyzer.by_model import (
    build_by_model_payload,
    build_denominators,
    normalize_entries,
)
from oddish.evals.analyzer.core import build_roster
from oddish.evals.analyzer.schemas import (
    AnalyzerEvalConfig,
    AnalyzerEvalOutput,
    Finding,
)
from oddish.evals.primitives import SubAnalysis
from oddish.worker.probe_creds import revoke_probe_creds
from oddish.workers.jobs.handlers import AnalyzerJobHandler

logger = logging.getLogger(__name__)

_EMPTY_SECTIONS = {"bad": "", "good": "", "capabilities": "", "headroom": ""}

# reduce.txt's four sections -> the AnalyzerModel columns _store writes.
_SECTION_COLUMN = {
    "bad_failure_content": "bad",
    "good_failure_content": "good",
    "universal_capabilities_content": "capabilities",
    "headroom_analysis": "headroom",
}


def _read_cli_source() -> bytes:
    return (files("oddish") / "assets" / "oddish-query").read_bytes()


def _daytona_client() -> Any:
    # DAYTONA_API_KEY is env-only; there is no settings field for it (app.py:157).
    key = os.environ.get("DAYTONA_API_KEY", "")
    if not key:
        raise RuntimeError(
            "analyzer-sandbox: DAYTONA_API_KEY is unset; cannot provision a sandbox"
        )
    return RealDaytonaClient(api_key=key, snapshot=settings.analyzer_snapshot or None)


def _runtime() -> Any:
    return ClaudeCodeRuntime()


def _gather(
    rows: list[tuple[Any, str]],
) -> tuple[list[SubAnalysis], dict[str, str], dict[str, dict]]:
    """DB-only: subanalysis_from_trial reads the trial.analysis JSON column, and
    oracle context comes off the trial's agent -- neither touches S3.

    Also builds the host-owned Finding fields per trial. This is the only place
    the trial rows are in scope, so task_id/task_path have to be captured here.
    """
    from oddish.analyze.classifier import _get_trial_agent_context
    from oddish.evals.analyzer.bucketing import BUCKET_OF

    subs: list[SubAnalysis] = []
    oracle_by_trial: dict[str, str] = {}
    host_by_trial: dict[str, dict] = {}
    for trial, task_path in rows:
        sa = subanalysis_from_trial(trial, task_path)
        if sa is None:
            continue
        subs.append(sa)
        host_by_trial[sa.trial_id] = {
            "trajectory_link": sa.trajectory_link,
            "model": sa.model,
            "classification": sa.classification,
            "subtype": sa.subtype,
            "task_id": trial.task_id,
            "task_path": task_path,
        }
        if BUCKET_OF.get(sa.classification) == "bad":
            oracle = (_get_trial_agent_context(trial.agent) or "").strip()
            if oracle:
                oracle_by_trial[sa.trial_id] = oracle
    return subs, oracle_by_trial, host_by_trial


async def _resolve_api_creds(
    rows: list[tuple[Any, str]], analyzer_id: str
) -> tuple[str, str, str]:
    """Mint a read-scoped key so the in-sandbox CLI can reach the public API.

    Returns (key_id, api_base_url, api_key); the caller owns revoking key_id.
    """
    from oddish.worker.probe_creds import mint_probe_creds

    org_id = rows[0][0].org_id
    key_id, creds = await mint_probe_creds(org_id=org_id, trial_id=analyzer_id)
    return key_id, creds["ODDISH_API_BASE_URL"], creds["ODDISH_API_KEY"]


async def sandbox_eval_rows(
    rows: list[tuple[Any, str]], config: AnalyzerEvalConfig, analyzer_id: str
) -> AnalyzerEvalOutput:
    subs, oracle_by_trial, host_by_trial = _gather(rows)
    bad, good, breakdown = bucket_subanalyses(subs)
    counts = {"trials": len(rows), "bad": len(bad), "good": len(good)}
    logger.info("analyzer-sandbox: bucketed %s breakdown=%s", counts, breakdown)

    if not bad and not good:
        return AnalyzerEvalOutput(
            sections=dict(_EMPTY_SECTIONS), findings=[], counts=counts,
            breakdown=breakdown, subanalyses=subs,
        )

    roster = build_roster(bad, good)
    # Checked up front, next to the DAYTONA_API_KEY check in _daytona_client:
    # without it the agent only fails once it is inside a provisioned sandbox,
    # after we have already minted a probe key and paid for two sandboxes.
    anthropic_key = settings.anthropic_api_key
    if not anthropic_key:
        raise RuntimeError(
            "analyzer-sandbox: anthropic_api_key is unset; the cohort agents "
            "cannot reach inference"
        )
    client, runtime, cli_src = _daytona_client(), _runtime(), _read_cli_source()
    key_id, api_base, api_key = await _resolve_api_creds(rows, analyzer_id)

    async def _cohort(bucket: str, cohort: list[SubAnalysis]):
        return await run_cohort(
            client, runtime, bucket=bucket, cohort=cohort, roster=roster,
            counts=counts, oracle_by_trial=oracle_by_trial,
            host_by_trial=host_by_trial,
            analyzer_id=analyzer_id, anthropic_key=anthropic_key,
            api_base=api_base, api_key=api_key, cli_src=cli_src,
            models_by_task=models_by_task_from_rows(rows),
            denominators=build_denominators(trial_model_rewards(rows), []),
        )

    jobs = [(b, c) for b, c in (("bad", bad), ("good", good)) if c]
    try:
        # return_exceptions=True: wait for every cohort to settle, successful
        # or not, so each one's own `finally` tears its sandbox down in a live
        # loop. Raising as soon as the first cohort fails would return control
        # to the caller (which stops the job right after), destroying the
        # event loop out from under the survivor's still-running sandbox --
        # its teardown `await` would then have no loop to run on, abandoning
        # the sandbox until Daytona's idle auto_stop reaps it.
        results = await asyncio.gather(
            *[_cohort(b, c) for b, c in jobs], return_exceptions=True
        )
    finally:
        await revoke_probe_creds(key_id, analyzer_id)

    first_exc = next((r for r in results if isinstance(r, BaseException)), None)
    if first_exc is not None:
        # Still no fallback to the API path on any cohort failure -- only the
        # timing of the failure changed, not whether the job fails.
        raise first_exc

    findings: list[Finding] = []
    sections = dict(_EMPTY_SECTIONS)
    by_model_entries: list[dict] = []
    comparisons: list[str] = []
    denominators = build_denominators(trial_model_rewards(rows), [])

    for (bucket, cohort), (cohort_findings, cohort_sections, cohort_by_model) in zip(
        jobs, results
    ):
        if not cohort_findings:
            raise RuntimeError(
                f"analyzer-sandbox: no findings from a non-empty {bucket!r} cohort "
                f"({len(cohort)} trials); refusing to persist blank sections"
            )
        findings.extend(cohort_findings)
        for key, text in cohort_sections.items():
            sections[_SECTION_COLUMN[key]] = text
        raw_entries, comparison = cohort_by_model
        # bucket= tags each entry, so one model appearing in both cohorts yields
        # two entries rather than one silently overwriting the other.
        by_model_entries.extend(
            normalize_entries(raw_entries, denominators, bucket=bucket)
        )
        if comparison.strip():
            comparisons.append(comparison.strip())

    by_model = build_by_model_payload(
        by_model_entries,
        "\n\n".join(comparisons),
        build_denominators(trial_model_rewards(rows), findings),
    )

    logger.info("analyzer-sandbox: complete, %d findings", len(findings))
    return AnalyzerEvalOutput(
        sections=sections, findings=findings, counts=counts, breakdown=breakdown,
        subanalyses=subs, by_model=by_model,
    )


class SandboxAnalyzerJobHandler(AnalyzerJobHandler):
    """Same job, same reap safety -- only the eval strategy differs."""

    eval_rows_fn = staticmethod(sandbox_eval_rows)


def install_sandbox_analyzer_handler() -> bool:
    """Swap the core ANALYZER handler for the sandbox one. Returns whether it
    was installed, so the caller can log the mode."""
    from oddish.workers.jobs import register

    if not settings.analyzer_sandbox_enabled:
        return False
    register(SandboxAnalyzerJobHandler(), override=True)
    return True

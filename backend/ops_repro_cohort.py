"""One-off: reproduce a single cohort agent run with its log stream VISIBLE.

    cd backend
    uv run modal run -e main ops_repro_cohort.py --analyzer 8e672fc5 --bucket good
    uv run modal run -e main ops_repro_cohort.py --analyzer 8e672fc5 --bucket good --limit 10

Why this exists: the prod worker drops every analyzer `logger.info` (no
basicConfig anywhere in backend/, so the root logger sits at WARNING). This
calls logging.basicConfig(level=INFO) in our own container FIRST, then drives
run_analyzer_blocks() directly, so the per-event agent stream from
analyzer_block_runner.py
logs and prod discards prints straight to `modal run` stdout, along with the
final [result:...] event that names the stop reason.

`modal run` mounts LOCAL source, so this runs the checked-out tree -- including
a17e91d2's richer CohortParseError (`stream tail=`), which prod predates. No
deploy needed to get the diagnostic.

Runs ONE bucket only (bad already succeeds; paying for it proves nothing).
Writes nothing to the analyzers row -- it drives the cohort in isolation.

--limit N truncates the cohort to N trials: if the failure is context/turn
exhaustion, a small N should SUCCEED where the full 97 fails. That contrast is
the actual proof.

Import only modal_app -- importing endpoints/worker registers the dispatcher.
"""

from __future__ import annotations

import modal

from modal_app import image, runtime_secrets

app = modal.App("repro-cohort-oneoff")


@app.function(image=image, secrets=runtime_secrets, timeout=3600)
async def repro(analyzer_id: str, bucket: str, limit: int) -> None:
    import logging

    # BEFORE importing the analyzer modules, so their module-level loggers
    # inherit a root that actually emits. This single line is the difference
    # between a visible agent stream and prod's silence.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    from oddish.config import Settings

    Settings.db_use_null_pool = True

    from sqlalchemy import text

    from api.services.blocks.analyzer.analyzer_block_runner import run_analyzer_blocks
    from oddish.db import get_session
    from oddish.evals.analyzer.bucketing import bucket_subanalyses
    from oddish.evals.analyzer.core import build_roster
    from oddish.workers.queue.analyzer_handler import _gather_trial_rows
    from worker.analyzer_sandbox import (
        _gather,
        _read_cli_source,
        _resolve_api_creds,
    )
    from oddish.config import settings
    from oddish.worker.probe_creds import revoke_probe_creds

    async with get_session() as session:
        org_id = (
            await session.execute(
                text("SELECT org_id FROM analyzers WHERE id = :aid"),
                {"aid": analyzer_id},
            )
        ).scalar_one()
        rows = await _gather_trial_rows(session, analyzer_id, org_id)

    subs, oracle_by_trial, host_by_trial = _gather(rows)
    bad, good, breakdown = bucket_subanalyses(subs)
    counts = {"trials": len(rows), "bad": len(bad), "good": len(good)}
    roster = build_roster(bad, good)

    cohort = {"bad": bad, "good": good}[bucket]
    if not cohort:
        print(f"bucket {bucket!r} is empty; nothing to reproduce.")
        return
    if limit:
        cohort = cohort[:limit]

    print(f"counts={counts} breakdown={breakdown}")
    print(
        f"driving bucket={bucket!r} with {len(cohort)} trial(s)"
        f"{' (TRUNCATED)' if limit else ' (full cohort)'}\n"
    )

    anthropic_key = settings.anthropic_api_key
    if not anthropic_key:
        raise RuntimeError("anthropic_api_key unset")
    cli_src = _read_cli_source()
    key_id, api_base, api_key = await _resolve_api_creds(rows, analyzer_id)
    try:
        findings, sections, _by_model = await run_analyzer_blocks(
            bucket=bucket,
            cohort=cohort,
            roster=roster,
            counts=counts,
            oracle_by_trial=oracle_by_trial,
            host_by_trial=host_by_trial,
            analyzer_id=analyzer_id,
            anthropic_key=anthropic_key,
            api_base=api_base,
            api_key=api_key,
            cli_src=cli_src,
            parallelism=16,
        )
    except Exception as exc:
        print(f"\n=== COHORT FAILED: {type(exc).__name__}: {exc}")
        raise
    else:
        print(
            f"\n=== COHORT OK: {len(findings)} findings, "
            f"sections={ {k: len(v) for k, v in sections.items()} }"
        )
    finally:
        await revoke_probe_creds(key_id, analyzer_id)


@app.local_entrypoint()
def main(analyzer: str, bucket: str = "good", limit: int = 0) -> None:
    repro.remote(analyzer_id=analyzer, bucket=bucket, limit=limit)

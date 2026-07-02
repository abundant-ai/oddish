"""Backfill a trial's analysis summary by running the canonical analysis job
inside Modal (with the oddish-prod secret).

Dry-run validates the trial (config branch + that artifacts download/extract)
without writing. Pass --execute to actually run run_analysis_job, which writes
trial.analysis + analysis_status. Idempotent: run_analysis_job skips trials
already SUCCESS/FAILED.

Usage:
    modal run backend/scripts/backfill_analysis.py --trial-id <id>            # dry-run
    modal run backend/scripts/backfill_analysis.py --trial-id <id> --execute  # write
"""

from pathlib import Path

import modal

# Repo root, used only locally for ``add_local_dir`` when building the image.
# Guarded because Modal re-imports this module INSIDE the container (as
# ``/root/backfill_analysis.py``), where ``parents[2]`` doesn't exist and would
# crash the container at import before the function ever runs. The fallback
# value is never used at runtime (the image is built locally).
_parents = Path(__file__).resolve().parents
REPO = _parents[2] if len(_parents) > 2 else Path("/")

app = modal.App("oddish-analysis-backfill")
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("git", "curl")
    # Claude CLI: the regular TrialClassifier branch shells out to `claude -p`.
    # (The probe branch uses the Bedrock SDK and doesn't need it, but install
    # it so either analyzer branch works.)
    .run_commands(
        "curl -fsSL https://claude.ai/install.sh | bash",
        "ln -sf /root/.local/bin/claude /usr/local/bin/claude",
    )
    .add_local_dir(
        str(REPO / "oddish"),
        remote_path="/oddish",
        copy=True,
        ignore=[".venv/", ".git/"],
    )
    # uv respects [tool.uv.sources] (harbor installs from its git fork).
    # ``[worker]`` pulls the server extra (sqlalchemy/asyncpg/aioboto3 for the
    # DB + S3) plus anthropic, which ``run_probe_analyzer`` needs for the
    # Bedrock call. A bare ``.`` only installs base CLI deps and the function
    # crashes importing sqlalchemy.
    .run_commands("pip install uv", "cd /oddish && uv pip install --system '.[worker]'")
)
secret = modal.Secret.from_name("oddish-prod", environment_name="main")

DEFAULT_TRIAL_ID = "anyio-subinterpreter-worker-cleanup-f2a9219c-25"


@app.function(image=image, secrets=[secret], timeout=900)
async def backfill(trial_id: str, execute: bool) -> None:
    from oddish.config import settings
    from oddish.db.storage import resolve_trial_directory, _cleanup_temp_directory
    from oddish.worker.probe_analysis import extract_probe_artifacts
    from oddish.workers.queue.analysis_handler import run_analysis_job
    from oddish.workers.queue.db_helpers import _trial_session

    # --- inspect ---
    async with _trial_session(trial_id, allow_missing=True) as (_s, trial):
        if trial is None:
            print(f"NO TRIAL: {trial_id}")
            return
        hc = trial.harbor_config or {}
        is_probe = hc.get("mode") == "probe" or bool(hc.get("extra_instructions"))
        print(f"trial            {trial.id}")
        print(f"status           {trial.status}")
        print(f"reward           {trial.reward}")
        print(f"analysis_status  {trial.analysis_status}")
        print(f"has_analysis     {trial.analysis is not None}")
        print(f"mode             {hc.get('mode')}")
        print(f"has_extra_instr  {bool(hc.get('extra_instructions'))}")
        print(f"result_focus     {hc.get('result_focus')!r}")
        print(f"eval_metric      {hc.get('evaluation_metric')!r}")
        print(
            f"=> branch        {'PROBE analyzer' if is_probe else 'regular TrialClassifier'}"
        )
        s3_key = trial.trial_s3_key
        result_path = trial.harbor_result_path

    # --- validate artifacts are readable (no write) ---
    trial_dir, temp_dir, _ = await resolve_trial_directory(
        trial_id=trial_id, trial_s3_key=s3_key, trial_result_path=result_path
    )
    try:
        arts = extract_probe_artifacts(trial_dir)
        vs = arts.get("verifier_stdout") or ""
        print(
            f"artifacts        agent_messages={len(arts.get('agent_messages') or [])}  "
            f"verifier_stdout={len(vs)} chars  trajectory={'yes' if arts.get('trajectory') else 'no'}"
        )
    finally:
        if temp_dir is not None:
            _cleanup_temp_directory(temp_dir)

    if not execute:
        print("\nDRY RUN — no write. Re-run with --execute to backfill.")
        return

    print("\nExecuting run_analysis_job …")
    await run_analysis_job(trial_id, settings.get_analysis_queue_key())

    async with _trial_session(trial_id, allow_missing=True) as (_s, trial):
        analysis = trial.analysis or {}
        print(f"\nAFTER  analysis_status={trial.analysis_status}")
        print(f"AFTER  headline={analysis.get('headline')!r}")
        print(f"AFTER  hypotheses={analysis.get('hypotheses')!r}")


@app.function(image=image, secrets=[secret], timeout=3600)
async def batch(task_names: str, execute: bool) -> None:
    """Backfill probe analysis for every eligible probe trial across a set of
    task NAMES (comma-separated, e.g. "implement-comm,implement-cut").

    A trial is eligible when it is a live (non-superseded) probe trial with S3
    artifacts whose analysis_status is anything but SUCCESS (NULL or FAILED).
    classify_trial_and_store skips trials already SUCCESS/FAILED, so for each
    eligible trial we first RESET analysis_status to NULL, then re-run the
    analyzer (which now uses the fixed extra_body/output_config code).
    """
    import os

    import asyncpg

    from oddish.db import utcnow  # noqa: F401  (kept for parity / future use)
    from oddish.workers.queue.analysis_handler import classify_trial_and_store
    from oddish.workers.queue.db_helpers import _trial_session

    names = [n.strip() for n in task_names.split(",") if n.strip()]
    if not names:
        print("No task names given.")
        return

    url = os.environ["ODDISH_DATABASE_URL"].replace("+asyncpg", "")
    conn = await asyncpg.connect(url, statement_cache_size=0)
    try:
        rows = await conn.fetch(
            """
            SELECT t.id,
                   t.task_id,
                   t.status::text          AS status,
                   t.reward,
                   t.analysis_status::text AS analysis_status,
                   (t.analysis IS NOT NULL) AS has_analysis,
                   (t.trial_s3_key IS NOT NULL) AS has_s3
            FROM   trials t
            WHERE  t.deleted_at IS NULL
              AND  t.superseded_by_trial_id IS NULL
              AND  ( (t.harbor_config->>'extra_instructions') IS NOT NULL
                     OR t.harbor_config->>'mode' = 'probe' )
              AND  EXISTS (
                     SELECT 1 FROM unnest($1::text[]) AS nm
                     WHERE t.task_id = nm
                        OR t.task_id ~ ('^' || nm || '-[0-9a-f]{6,}$')
                   )
            ORDER BY t.task_id, t.id
            """,
            names,
        )
    finally:
        await conn.close()

    # Per-name match counts (so a typo'd name that matched nothing is visible).
    matched_by_name: dict[str, int] = {n: 0 for n in names}
    import re

    for r in rows:
        for n in names:
            if r["task_id"] == n or re.match(
                rf"^{re.escape(n)}-[0-9a-f]{{6,}}$", r["task_id"]
            ):
                matched_by_name[n] += 1
                break

    eligible = [
        r for r in rows if r["has_s3"] and (r["analysis_status"] or "") != "SUCCESS"
    ]

    print(f"=== matched {len(rows)} probe trial(s) across {len(names)} name(s) ===")
    for n in names:
        flag = "" if matched_by_name[n] else "   <-- NO MATCH"
        print(f"  {n:32} probe_trials={matched_by_name[n]}{flag}")
    print(
        f"\n=== {len(eligible)} eligible for backfill (has_s3, analysis != SUCCESS) ==="
    )
    for r in eligible:
        print(
            f"  {r['id']:42} status={r['status']} reward={r['reward']} "
            f"analysis={r['analysis_status']} has_analysis={r['has_analysis']}"
        )
    skipped_success = [r for r in rows if (r["analysis_status"] or "") == "SUCCESS"]
    skipped_no_s3 = [r for r in rows if not r["has_s3"]]
    if skipped_success:
        print(f"\n  ({len(skipped_success)} already SUCCESS — skipped)")
    if skipped_no_s3:
        print(f"\n  ({len(skipped_no_s3)} have no S3 artifacts — cannot backfill):")
        for r in skipped_no_s3:
            print(
                f"    {r['id']:42} status={r['status']} reward={r['reward']} "
                f"analysis={r['analysis_status']}"
            )

    if not execute:
        print("\nDRY RUN — no write. Re-run with --execute to backfill.")
        return

    print(f"\nExecuting backfill for {len(eligible)} trial(s)…\n")
    ok = failed = 0
    for r in eligible:
        tid = r["id"]
        # Reset so classify_trial_and_store doesn't short-circuit on FAILED.
        async with _trial_session(tid, allow_missing=True) as (session, trial):
            if trial is None:
                print(f"  MISSING  {tid}")
                continue
            trial.analysis = None
            trial.analysis_status = None
            trial.analysis_error = None
            trial.analysis_started_at = None
            trial.analysis_finished_at = None
            await session.commit()
        try:
            await classify_trial_and_store(tid)
        except Exception as exc:  # noqa: BLE001 — keep going across the batch
            failed += 1
            print(f"  ERROR    {tid}: {type(exc).__name__}: {exc}")
            continue
        async with _trial_session(tid, allow_missing=True) as (_s, trial):
            analysis = (trial.analysis or {}) if trial else {}
            status = trial.analysis_status if trial else "?"
            if str(status).endswith("SUCCESS"):
                ok += 1
            else:
                failed += 1
            print(
                f"  {str(status).split('.')[-1]:8} {tid}  "
                f"headline={analysis.get('headline')!r}"
            )

    print(f"\n=== done: {ok} succeeded, {failed} failed, {len(eligible)} attempted ===")


@app.local_entrypoint()
def main(
    trial_id: str = "",
    task_names: str = "",
    execute: bool = False,
) -> None:
    if task_names:
        batch.remote(task_names, execute)
    elif trial_id:
        backfill.remote(trial_id, execute)
    else:
        print("Pass --trial-id <id> for one trial, or --task-names a,b,c for a batch.")

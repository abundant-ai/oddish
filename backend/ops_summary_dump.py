"""Dump production trajectory summaries for a trial, task, or experiment.

Runs the production summary path (``build_summary_block``, shared with
``summarize_trajectory.generate()``) against prod DB + S3 from inside Modal,
and writes one full block record per trial to a local directory:

    cd backend
    uv run modal run -e main ops_summary_dump.py --experiment exp_123 --limit 8 --out ./summaries
    uv run modal run -e main ops_summary_dump.py --task my-task --limit 5 --out ./summaries
    uv run modal run -e main ops_summary_dump.py --trials tr_a,tr_b --out ./summaries

Each record carries the prompt, the raw model text, and the parsed summary, so
a downstream taxonomy test script can re-parse offline without re-calling the
API. Nothing is persisted to ``analyzer_blocks`` or S3 unless --persist.

``backend/.env`` must be ABSENT for these runs. ``modal_app`` reads it and
appends it as a ``Secret.from_dict`` *after* the oddish-prod secret, so a dev
dotenv's ODDISH_DATABASE_URL wins inside the container and the run dies on
``ConnectionRefused 127.0.0.1:5432`` instead of reading prod. Move it aside for
the run and restore it afterwards.

``index.json`` describes the LAST run's cohort only, even with ``--append``
(per-trial files accumulate; the index does not). A non-empty ``--out`` is
refused unless ``--append`` is passed, so a run can't silently overwrite a
directory holding a different cohort's files. Use a fresh ``--out`` per
cohort if a downstream script enumerates via the index.

Imports only ``modal_app``, so no scheduled worker or reconciler functions are
registered.
"""

from __future__ import annotations

import json
from pathlib import Path

from modal_app import app, image, runtime_secrets


@app.function(image=image, secrets=runtime_secrets, timeout=3600)
async def dump(
    trials: str = "",
    task: str = "",
    experiment: str = "",
    limit: int = 0,
    model: str = "",
    persist: bool = False,
) -> dict:
    from oddish.config import Settings

    # Short-lived read-mostly job; avoid adding a warm pooler connection.
    Settings.db_use_null_pool = True

    from api.services.summarize_trajectory import load_summary_prompt_template
    from api.services.summary_dump import run_cohort
    from oddish.db import get_session

    trial_ids = [t.strip() for t in trials.split(",") if t.strip()]
    async with get_session() as session:
        # Read the packaged template up front -- fail fast before spending
        # time resolving the cohort.
        load_summary_prompt_template()
        result = await run_cohort(
            session,
            trials=trial_ids or None,
            task=task or None,
            experiment=experiment or None,
            limit=limit,
            model=model or None,
            persist=persist,
        )
        # CohortResult isn't JSON-serializable over the Modal RPC boundary as-is.
        return {"records": list(result), "skipped": result.skipped}


@app.local_entrypoint()
def main(
    trials: str = "",
    task: str = "",
    experiment: str = "",
    limit: int = 0,
    out: str = "./summaries",
    model: str = "",
    persist: bool = False,
    append: bool = False,
) -> None:
    # Validate locally first -- a forgotten scope flag should fail immediately,
    # not after a ~10-minute Modal cold start.
    from api.services.summary_dump import validate_scope

    trial_ids = [t.strip() for t in trials.split(",") if t.strip()]
    validate_scope(trials=trial_ids or None, task=task or None, experiment=experiment or None)

    out_dir = Path(out)
    if out_dir.exists() and any(out_dir.iterdir()) and not append:
        print(
            f"refusing to write into non-empty output directory {out_dir} -- "
            "pass --append to add to it"
        )
        return

    result = dump.remote(
        trials=trials, task=task, experiment=experiment,
        limit=limit, model=model, persist=persist,
    )
    records = result["records"]
    skipped = result["skipped"]

    out_dir.mkdir(parents=True, exist_ok=True)
    for i, record in enumerate(records):
        # trial_id can be None on a defensive failure path; fall back to a
        # per-record name so two such failures don't collide.
        name = record["trial_id"] or f"unknown-{i}"
        (out_dir / f"{name}.json").write_text(json.dumps(record, indent=2))

    index = {
        "scope": {
            "trials": trials or None, "task": task or None,
            "experiment": experiment or None, "limit": limit or None,
        },
        "model": model or (records[0]["model"] if records else None),
        "persisted": persist,
        "trials": [
            {"trial_id": r["trial_id"], "status": r["status"], "error": r["error"]}
            for r in records
        ],
        "skipped": skipped,
    }
    (out_dir / "index.json").write_text(json.dumps(index, indent=2))

    ok = sum(1 for r in records if r["status"] == "success")
    print(f"\nwrote {len(records)} record(s) to {out_dir} ({ok} success, {len(skipped)} skipped)")

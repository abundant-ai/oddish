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

``index.json`` describes the LAST run only, while per-trial files accumulate in
the output directory. Use a fresh --out per cohort if a downstream script
enumerates via the index.

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
) -> list[dict]:
    from oddish.config import Settings

    # Short-lived read-mostly job; avoid adding a warm pooler connection.
    Settings.db_use_null_pool = True

    from api.services.summary_dump import run_cohort
    from oddish.db import get_session

    trial_ids = [t.strip() for t in trials.split(",") if t.strip()]
    async with get_session() as session:
        return await run_cohort(
            session,
            trials=trial_ids or None,
            task=task or None,
            experiment=experiment or None,
            limit=limit,
            model=model or None,
            persist=persist,
        )


@app.local_entrypoint()
def main(
    trials: str = "",
    task: str = "",
    experiment: str = "",
    limit: int = 0,
    out: str = "./summaries",
    model: str = "",
    persist: bool = False,
) -> None:
    records = dump.remote(
        trials=trials, task=task, experiment=experiment,
        limit=limit, model=model, persist=persist,
    )

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for record in records:
        (out_dir / f"{record['trial_id']}.json").write_text(
            json.dumps(record, indent=2)
        )

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
    }
    (out_dir / "index.json").write_text(json.dumps(index, indent=2))

    ok = sum(1 for r in records if r["status"] == "success")
    print(f"\nwrote {len(records)} record(s) to {out_dir} ({ok} success)")

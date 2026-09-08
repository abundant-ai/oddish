"""Repair an audited set of terminal worker jobs' stale retrying trial mirrors.

Default is read-only. Pass an explicit JSON array of trial IDs and --apply to
commit repairs. Each batch rechecks current state under locks; no work is
enqueued and no attempt budget is increased. Credentials stay inside Modal.

    modal run backend/scripts/reconcile_retry_trials.py --org-id ORG \
      --trial-ids-file /path/ids.json --output /path/preview.json
"""

import json
from pathlib import Path

import modal

REPO = Path(__file__).resolve().parents[2] if modal.is_local() else Path("/repo")
app = modal.App("oddish-retry-reconciliation")
image = (
    modal.Image.debian_slim(python_version="3.13")
    .add_local_dir(
        str(REPO / "oddish"),
        "/repo/oddish",
        copy=True,
        ignore=[".venv/", ".git/", "tests/"],
    )
    .run_commands("python -m pip install '/repo/oddish[server]'")
)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("oddish-prod", environment_name="main")],
    timeout=180,
)
async def reconcile(org_id: str, trial_ids: list[str], apply: bool):
    from sqlalchemy import text
    from oddish.core.retry_reconciliation import reconcile_terminal_retry_trials
    from oddish.db import get_session

    async with get_session() as session:
        if not apply:
            await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '60s'"))
        return json.dumps(
            await reconcile_terminal_retry_trials(
                session,
                org_id=org_id,
                trial_ids=trial_ids,
                limit=len(trial_ids),
                dry_run=not apply,
            ),
            default=str,
        )


@app.local_entrypoint()
def main(org_id: str, trial_ids_file: str, output: str, apply: bool = False):
    trial_ids = json.loads(Path(trial_ids_file).read_text())
    if not isinstance(trial_ids, list) or not all(
        isinstance(x, str) for x in trial_ids
    ):
        raise ValueError("trial-ids-file must contain a JSON array of trial IDs")
    if len(set(trial_ids)) != len(trial_ids):
        raise ValueError("trial IDs must be unique")
    path = Path(output)
    if path.exists():
        raise ValueError("Choose a new output path to preserve previous audit evidence")
    changes = []
    for start in range(0, len(trial_ids), 50):
        batch = json.loads(
            reconcile.remote(org_id, trial_ids[start : start + 50], apply)
        )
        changes.extend(batch)
        path.write_text(
            json.dumps(
                {"applied": apply, "org_id": org_id, "changes": changes}, indent=2
            )
        )
        print(
            f"{'Repaired' if apply else 'Would repair'} {len(batch)} records; total {len(changes)}"
        )

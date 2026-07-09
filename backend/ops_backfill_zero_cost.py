"""One-off: re-price cost_usd=0 / NULL trials with tokens, in the prod runtime.

Runs the shared ``oddish.backfill_zero_cost`` script in a Modal container that
mounts the ``oddish-prod`` secret, so ``ODDISH_DATABASE_URL`` is read from the
secret and never handled locally.

    cd backend
    modal run -e main ops_backfill_zero_cost.py           # dry run (read-only)
    modal run -e main ops_backfill_zero_cost.py --apply   # write

Ephemeral: importing only ``modal_app`` (not ``endpoints``/``worker``) means the
scheduled dispatcher/reconciler functions are never registered, so this cannot
spin up a second dispatcher. It runs the backfill once and exits.
"""

from modal_app import app, image, runtime_secrets


@app.function(image=image, secrets=runtime_secrets, timeout=3600)
def backfill(apply: bool = False) -> None:
    import asyncio

    from oddish.config import Settings

    # Transient per-op connections, Supavisor/PgBouncer-safe (same as workers).
    Settings.db_use_null_pool = True
    from oddish.backfill_zero_cost import run_backfill

    asyncio.run(run_backfill(apply=apply))


@app.local_entrypoint()
def main(apply: bool = False) -> None:
    backfill.remote(apply=apply)

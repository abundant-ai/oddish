"""One-time deployment gate for historical CTRF summaries."""

from modal_app import app, image, runtime_secrets


@app.function(image=image, secrets=runtime_secrets, timeout=3600)
def backfill(apply: bool = False) -> None:
    import asyncio

    from oddish.config import Settings

    Settings.db_use_null_pool = True
    from oddish.backfill_verifier_summaries import run_backfill

    asyncio.run(run_backfill(apply=apply))


@app.local_entrypoint()
def main(apply: bool = False) -> None:
    backfill.remote(apply=apply)

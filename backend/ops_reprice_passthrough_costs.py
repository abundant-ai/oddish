"""One-off historical Claude-Code passthrough cost reprice in prod runtime.

    cd backend
    modal run -e main ops_reprice_passthrough_costs.py \
        --since 2026-01-01
    modal run -e main ops_reprice_passthrough_costs.py \
        --since 2026-01-01 --provider fireworks --apply

Dry-run is the default. Importing only ``modal_app`` avoids registering any
scheduled dispatcher or reconciler functions.
"""

from modal_app import app, image, runtime_secrets


@app.function(image=image, secrets=runtime_secrets, timeout=3600)
def reprice(
    since: str,
    apply: bool = False,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    import asyncio

    from oddish.config import Settings

    Settings.db_use_null_pool = True
    from oddish.reprice_passthrough_costs import run_reprice

    asyncio.run(
        run_reprice(
            since=since,
            apply=apply,
            model=model,
            provider=provider,
        )
    )


@app.local_entrypoint()
def main(
    since: str,
    apply: bool = False,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    reprice.remote(
        since=since,
        apply=apply,
        model=model,
        provider=provider,
    )

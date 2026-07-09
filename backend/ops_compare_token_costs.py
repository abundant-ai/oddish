"""Compare recorded cost with a token-rate estimate in the prod runtime.

Defaults are Fireworks MiniMax M3 standard serverless rates published in
2026-07: $0.30/M input, $0.06/M cached input, and $1.20/M output.

    cd backend
    modal run -e main ops_compare_token_costs.py --since 2026-07-01

This is read-only. It imports only ``modal_app``, so no scheduled worker or
reconciler functions are registered.
"""

from modal_app import app, image, runtime_secrets


@app.function(image=image, secrets=runtime_secrets, timeout=3600)
def compare(
    since: str,
    model: str = "fireworks/minimax-m3",
    input_rate: float = 0.30,
    cache_read_rate: float = 0.06,
    output_rate: float = 1.20,
    cache_write_rate: float = 0.30,
) -> None:
    from oddish.config import Settings

    # One transient read-only query; avoid adding a warm pooler connection.
    Settings.db_use_null_pool = True
    from oddish.compare_token_costs import TokenRates, run

    run(
        since=since,
        model=model,
        rates=TokenRates(
            input=input_rate,
            cache_read=cache_read_rate,
            cache_write=cache_write_rate,
            output=output_rate,
        ),
    )


@app.local_entrypoint()
def main(
    since: str,
    model: str = "fireworks/minimax-m3",
    input_rate: float = 0.30,
    cache_read_rate: float = 0.06,
    output_rate: float = 1.20,
    cache_write_rate: float = 0.30,
) -> None:
    compare.remote(
        since=since,
        model=model,
        input_rate=input_rate,
        cache_read_rate=cache_read_rate,
        output_rate=output_rate,
        cache_write_rate=cache_write_rate,
    )

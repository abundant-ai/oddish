"""Compare recorded cost with a token-rate estimate in the prod runtime.

Supply the model and all USD-per-million-token rates explicitly; prices change.

    cd backend
    modal run ops_compare_token_costs.py --since <YYYY-MM-DD> --model <provider/model> \
      --input-rate <rate> --cache-read-rate <rate> --output-rate <rate> --cache-write-rate <rate>

This is read-only. It imports only ``modal_app``, so no scheduled worker or
reconciler functions are registered.
"""

import modal

from modal_app import image, runtime_secrets

app = modal.App("oddish-compare-token-costs")


@app.function(image=image, secrets=runtime_secrets, timeout=3600)
def compare(
    since: str,
    model: str,
    input_rate: float,
    cache_read_rate: float,
    output_rate: float,
    cache_write_rate: float,
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
    model: str,
    input_rate: float,
    cache_read_rate: float,
    output_rate: float,
    cache_write_rate: float,
) -> None:
    compare.remote(
        since=since,
        model=model,
        input_rate=input_rate,
        cache_read_rate=cache_read_rate,
        output_rate=output_rate,
        cache_write_rate=cache_write_rate,
    )

"""Ask a closing preview's backend to delete its own GKE cluster.

Run via ``modal run`` from stop_preview.sh, BEFORE ``modal app stop``: the
deployed ``teardown_gke_cluster`` function holds the cloud credentials this
workflow does not have, and it dies with the app. Previews deployed before
that function existed, and GKE-less previews, simply have nothing to call --
both are a skip, never a failure.
"""

from __future__ import annotations

import os

import modal

app = modal.App("preview-gke-teardown-helper")


@app.local_entrypoint()
def main(app_name: str) -> None:
    environment = os.environ.get("MODAL_ENVIRONMENT")
    try:
        fn = modal.Function.from_name(
            app_name, "teardown_gke_cluster", environment_name=environment
        )
        print(f"gke teardown: {fn.remote()}")
    except modal.exception.NotFoundError:
        print("gke teardown: skip (app has no teardown function)")

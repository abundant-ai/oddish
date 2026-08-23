"""Ask a closing preview's backend to delete its own GKE cluster.

Run via ``modal run`` from stop_preview.sh, BEFORE ``modal app stop``: the
deployed ``teardown_gke_cluster`` function holds the cloud credentials this
workflow does not have, and it dies with the app. Previews deployed before
that function existed, and GKE-less previews, simply have nothing to call --
both are a skip, never a failure.
"""

from __future__ import annotations

import os
import sys
import time

import modal

app = modal.App("preview-gke-teardown-helper")

_ATTEMPTS = 3
_BACKOFF_SEC = 10.0


@app.local_entrypoint()
def main(app_name: str) -> None:
    environment = os.environ.get("MODAL_ENVIRONMENT")
    try:
        fn = modal.Function.from_name(
            app_name, "teardown_gke_cluster", environment_name=environment
        )
    except modal.exception.NotFoundError:
        print("gke teardown: skip (app has no teardown function)")
        return
    # A missing function is the only skippable shape; the remote call itself
    # failing means the cluster may still exist, and the caller is about to
    # stop the one app whose scheduled reaper could still delete it. Retry
    # here, and hand a real failure to the caller as a distinct exit code so
    # it can keep that owner alive instead of stopping it blind.
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            print(f"gke teardown: {fn.remote()}")
            return
        except modal.exception.NotFoundError:
            # Function.from_name is lazy: a missing function surfaces HERE,
            # on the first call, not at lookup. Same skip as above.
            print("gke teardown: skip (app has no teardown function)")
            return
        except Exception as exc:  # noqa: BLE001 -- classified by the caller
            last = exc
            print(
                f"gke teardown: attempt {attempt}/{_ATTEMPTS} failed: {exc}",
                file=sys.stderr,
            )
            if attempt < _ATTEMPTS:
                time.sleep(_BACKOFF_SEC)
    print(
        f"::error::GKE cluster teardown failed after {_ATTEMPTS} attempts: {last}",
        file=sys.stderr,
    )
    sys.exit(2)

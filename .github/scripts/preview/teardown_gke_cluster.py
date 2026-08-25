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
# The remote function has its own 10-minute execution cap, but callers run
# inside larger workflows with other required work. Waiting for that full cap
# (and then retrying) can consume the preview database job's 20-minute budget.
# Five minutes covers the remote teardown's ordinary 240-second delete wait
# plus startup slack. On expiry the specific remote call is cancelled and the
# caller preserves the old app so its scheduled reaper retains ownership.
_CALL_TIMEOUT_SEC = 300.0


def _invoke_teardown(
    fn,
    *,
    attempts: int = _ATTEMPTS,
    backoff_sec: float = _BACKOFF_SEC,
    call_timeout_sec: float = _CALL_TIMEOUT_SEC,
) -> str | None:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            call = fn.spawn()
            return call.get(timeout=call_timeout_sec)
        except modal.exception.NotFoundError:
            # Function.from_name is lazy: a missing function surfaces on the
            # first call rather than at lookup.
            return None
        except modal.exception.TimeoutError as exc:
            # Do not spawn a duplicate deletion. The first remote call may
            # keep ``modal run`` alive after get() times out, so cancel that
            # call before returning failure. The caller retains the old app
            # and its scheduled reaper instead of stopping it without proof
            # that the cluster is gone.
            call.cancel()
            raise RuntimeError(
                f"GKE cluster teardown did not finish within "
                f"{call_timeout_sec:g}s; timed-out call was cancelled"
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- classified by the caller
            last = exc
            print(
                f"gke teardown: attempt {attempt}/{attempts} failed: {exc}",
                file=sys.stderr,
            )
            if attempt < attempts:
                time.sleep(backoff_sec)
    raise RuntimeError(
        f"GKE cluster teardown failed after {attempts} attempts: {last}"
    )


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
    # A missing function is the only skippable shape. A failed or timed-out
    # remote call means the cluster may still exist, so return a distinct
    # failure and let the caller keep the app's scheduled reaper alive.
    try:
        outcome = _invoke_teardown(fn)
    except RuntimeError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(2)
    if outcome is None:
        print("gke teardown: skip (app has no teardown function)")
        return
    print(f"gke teardown: {outcome}")

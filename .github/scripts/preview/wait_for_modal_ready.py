"""Poll the freshly deployed Modal preview API's /openapi.json until
it serves a 200 with a non-empty body, so the workflow asserts the
deploy actually came up before marking the job green.

    python wait_for_modal_ready.py <base_url>

Exits non-zero on timeout so a subsequent `if: failure()` step can
dump container logs for diagnosis.
"""

import sys
import time
import urllib.error
import urllib.request

TIMEOUT_S = 360
# Must exceed a cold start of the backend image: the first request after a
# deploy boots a container that imports the whole app, which has measured at
# ~30s. A shorter per-request timeout abandons every attempt mid-boot and the
# poll can never succeed no matter how long TIMEOUT_S is.
REQUEST_TIMEOUT_S = 120
POLL_INTERVAL_S = 5


def wait_for_ready(
    base_url,
    timeout_s=TIMEOUT_S,
    request_timeout_s=REQUEST_TIMEOUT_S,
    poll_interval_s=POLL_INTERVAL_S,
):
    readiness_url = f"{base_url.rstrip('/')}/openapi.json"
    deadline = time.monotonic() + timeout_s
    last_error = "no readiness response yet"

    while True:
        try:
            with urllib.request.urlopen(
                readiness_url, timeout=request_timeout_s
            ) as response:
                payload = response.read(64)
            if response.status == 200 and payload:
                print(f"Modal preview ready at {readiness_url}")
                return
            last_error = f"unexpected response: {payload!r}"
        except urllib.error.HTTPError as exc:
            last_error = f"http {exc.code}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval_s)

    raise SystemExit(
        f"Modal preview never became ready at {readiness_url}: {last_error}"
    )


if __name__ == "__main__":
    wait_for_ready(sys.argv[1])

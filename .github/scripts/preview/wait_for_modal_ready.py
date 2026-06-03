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

TIMEOUT_S = 180
POLL_INTERVAL_S = 5


def main():
    base_url = sys.argv[1].rstrip("/")
    readiness_url = f"{base_url}/openapi.json"
    deadline = time.time() + TIMEOUT_S
    last_error = "no readiness response yet"

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(readiness_url, timeout=10) as response:
                payload = response.read(64)
            if response.status == 200 and payload:
                print(f"Modal preview ready at {readiness_url}")
                return
            last_error = f"unexpected response: {payload!r}"
        except urllib.error.HTTPError as exc:
            last_error = f"http {exc.code}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(POLL_INTERVAL_S)

    raise SystemExit(
        f"Modal preview never became ready at {readiness_url}: {last_error}"
    )


if __name__ == "__main__":
    main()

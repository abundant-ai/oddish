"""Poll the Modal preview API's openapi.json until it responds.

Usage:
    python wait_for_modal_ready.py <base_url>

Treats any non-empty 200 from `<base_url>/openapi.json` as ready. Times out
after 180 seconds.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

READINESS_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 5


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: wait_for_modal_ready.py <base_url>", file=sys.stderr)
        return 2

    base_url = argv[1].rstrip("/")
    readiness_url = f"{base_url}/openapi.json"
    deadline = time.time() + READINESS_TIMEOUT_SECONDS
    last_error = "no readiness response yet"

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(readiness_url, timeout=10) as response:
                payload = response.read(64)
            if response.status == 200 and payload:
                print(f"Modal preview ready at {readiness_url}")
                return 0
            last_error = f"unexpected response: {payload!r}"
        except urllib.error.HTTPError as exc:
            last_error = f"http {exc.code}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(POLL_INTERVAL_SECONDS)

    print(
        f"Modal preview never became ready at {readiness_url}: {last_error}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

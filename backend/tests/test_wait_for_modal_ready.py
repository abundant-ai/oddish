"""The preview readiness poll must outlast a backend cold start.

A per-request timeout shorter than the boot abandons every attempt
mid-flight, so the poll fails for the whole TIMEOUT_S window even
though the app comes up fine (four PRs failed this way on 2026-07-20).
"""

import http.server
import importlib.util
import threading
import time
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github/scripts/preview/wait_for_modal_ready.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("wait_for_modal_ready", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def slow_server():
    """Serves 200 on /openapi.json, but only after a boot delay."""

    class Handler(http.server.BaseHTTPRequestHandler):
        boot_delay_s = 1.0

        def do_GET(self):
            time.sleep(self.boot_delay_s)
            body = b'{"openapi":"3.1.0"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", Handler
    server.shutdown()


def test_ready_when_request_timeout_outlasts_boot(slow_server):
    url, _ = slow_server
    _load().wait_for_ready(
        url, timeout_s=10, request_timeout_s=5, poll_interval_s=0.1
    )


def test_short_request_timeout_never_succeeds(slow_server):
    """Pins the failure mode: the overall window is ample, but every
    individual request gives up before the server answers."""
    url, _ = slow_server
    with pytest.raises(SystemExit):
        _load().wait_for_ready(
            url, timeout_s=4, request_timeout_s=0.2, poll_interval_s=0.1
        )


def test_request_timeout_covers_observed_cold_start():
    mod = _load()
    # Measured cold start of the preview API is ~30s; the shipped default
    # must clear it with margin, and the overall window must fit >1 attempt.
    assert mod.REQUEST_TIMEOUT_S >= 60
    assert mod.TIMEOUT_S >= 2 * mod.REQUEST_TIMEOUT_S

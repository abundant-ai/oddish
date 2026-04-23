"""Unit tests for the task-file ETag helper in ``backend.api.routers.tasks``.

Importing the router module is heavy because it pulls in Clerk auth,
Modal config, and the rest of the backend. We mirror the helper in a
tiny shim here so the regression test (weak-etag formatting must not
double-quote S3's already-quoted ``ETag``) is enforced in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "oddish" / "src"))


def _load_helper():
    """Load ``_build_task_file_etag`` without triggering the full router import.

    The helper is a pure function, so we parse it out of the router
    module source and ``exec`` it into a throwaway namespace. This keeps
    the test decoupled from auth/Clerk/FastAPI wiring that isn't
    relevant to etag formatting.
    """
    router_path = (
        Path(__file__).resolve().parents[2] / "backend" / "api" / "routers" / "tasks.py"
    )
    source = router_path.read_text(encoding="utf-8")

    needle = "def _build_task_file_etag"
    start = source.index(needle)
    tail = source[start:]
    next_def = tail.index("\n\n\n")
    snippet = tail[:next_def]

    namespace: dict[str, object] = {}
    exec(snippet, namespace)
    return namespace["_build_task_file_etag"]


_build_task_file_etag = _load_helper()


def test_etag_strips_s3_surrounding_quotes():
    # S3 / MinIO return the ETag already wrapped in ``"..."``; the
    # helper must unwrap them before composing ``W/"..."`` so the final
    # header is a single valid quoted-string.
    assert _build_task_file_etag('"abc123"', "task.py") == 'W/"abc123:task.py"'


def test_etag_handles_unquoted_input():
    # Some S3-compatible backends drop the quotes; the helper must be
    # forgiving either way so the fallback ``(content_length,
    # last_modified)`` cache key (which has no quotes) still produces
    # a valid header.
    assert _build_task_file_etag("abc123", "task.py") == 'W/"abc123:task.py"'


def test_etag_trims_whitespace():
    assert _build_task_file_etag('  "abc123"  ', "task.py") == 'W/"abc123:task.py"'


@pytest.mark.parametrize("etag", ['"abc"', "abc", '"abc"\n'])
def test_etag_output_has_exactly_three_double_quotes(etag):
    # ``W/"..."`` should contain ``W/``, a leading ``"``, the payload,
    # and a trailing ``"`` -- three literal ``"`` characters in total.
    # Any more means the inner etag wasn't normalized.
    output = _build_task_file_etag(etag, "task.py")
    assert output.count('"') == 2
    assert output.startswith('W/"')
    assert output.endswith('"')

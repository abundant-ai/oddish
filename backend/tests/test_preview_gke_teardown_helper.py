"""The preview teardown helper cannot consume the whole deploy workflow."""

import importlib.util
from pathlib import Path

import modal
import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github/scripts/preview/teardown_gke_cluster.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("teardown_gke_cluster", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_teardown_uses_a_bounded_result_wait():
    observed = {}

    class Call:
        def get(self, *, timeout):
            observed["timeout"] = timeout
            return "deleted 1 cluster(s)"

    class Function:
        def spawn(self):
            observed["spawned"] = True
            return Call()

    outcome = _load()._invoke_teardown(Function(), call_timeout_sec=17)

    assert outcome == "deleted 1 cluster(s)"
    assert observed == {"spawned": True, "timeout": 17}


def test_timeout_does_not_spawn_a_duplicate_delete():
    spawned = 0

    class Call:
        def get(self, *, timeout):
            raise modal.exception.TimeoutError("still running")

    class Function:
        def spawn(self):
            nonlocal spawned
            spawned += 1
            return Call()

    with pytest.raises(RuntimeError, match="did not finish within 17s"):
        _load()._invoke_teardown(
            Function(), attempts=3, backoff_sec=0, call_timeout_sec=17
        )

    assert spawned == 1

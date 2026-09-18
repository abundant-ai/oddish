"""Protect the time until users see output, including Python/CLI startup."""

from __future__ import annotations

import os
from pathlib import Path
import select
import subprocess
import sys
import sysconfig
import time

import pytest


CASES = [
    (["--help"], b"Usage:"),
    (["--version"], b"oddish "),
    (["version", "--json"], b'"version"'),
    (["status", "--help"], b"Usage:"),
    (["run", "--help"], b"Usage:"),
    (["upload", "--help"], b"Usage:"),
]
# Generous headroom for shared CI runners; the deterministic test below catches
# the expensive import even on machines fast enough to fit it into this budget.
FIRST_OUTPUT_BUDGET_SECONDS = 3.0


def _environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ODDISH_", "LITELLM_"))
    }
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    # Exercise this checkout even when pytest runs in the backend environment.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    return env


def _entrypoint() -> str:
    return str(Path(sysconfig.get_path("scripts")) / "oddish")


@pytest.mark.parametrize("args,expected", CASES)
def test_cli_first_output(args, expected, tmp_path):
    # Start the clock before spawning Python, not after importing oddish.cli.
    # Read the real console script's stdout as a pipe, without forcing unbuffered
    # Python. Stderr cannot satisfy the deadline with a warning or traceback.
    with (tmp_path / "stderr").open("w+b") as stderr:
        start = time.monotonic()
        with subprocess.Popen(
            [sys.executable, _entrypoint(), *args],
            cwd=tmp_path,
            env=_environment(),
            stdout=subprocess.PIPE,
            stderr=stderr,
        ) as process:
            try:
                remaining = max(
                    0, FIRST_OUTPUT_BUDGET_SECONDS - (time.monotonic() - start)
                )
                ready, _, _ = select.select([process.stdout], [], [], remaining)
                first = os.read(process.stdout.fileno(), 1) if ready else b""
                elapsed = time.monotonic() - start
                assert first, f"No stdout within {FIRST_OUTPUT_BUDGET_SECONDS}s: {args}"
                assert elapsed < FIRST_OUTPUT_BUDGET_SECONDS, (args, elapsed)
                rest, _ = process.communicate(timeout=15)
                stderr.seek(0)
                errors = stderr.read()
                assert process.returncode == 0, errors.decode(errors="replace")
                assert expected in first + rest
                print(f"{args}: first stdout at {elapsed:.3f}s")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


_GUARDED_ENTRYPOINT = """
import importlib.abc
import runpy
import sys

network_events = []
def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        network_events.append(event)
        raise RuntimeError("CLI startup must not access the network")
sys.addaudithook(audit)

class NoLiteLLM(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "litellm" or fullname.startswith("litellm."):
            # BaseException prevents optional-import/fallback handlers swallowing
            # the failure and making the regression check falsely pass.
            raise BaseException("CLI startup must not import LiteLLM")
sys.meta_path.insert(0, NoLiteLLM())
sys.argv = sys.argv[1:]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
finally:
    assert not network_events, network_events
"""


@pytest.mark.parametrize("args,expected", CASES)
def test_cli_help_and_version_are_offline(args, expected, tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _GUARDED_ENTRYPOINT, _entrypoint(), *args],
        cwd=tmp_path,
        env=_environment(),
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert expected in result.stdout

"""The off-Modal portability proof (design spec §10, success criterion §2.1).

Two layers:

* ``test_worker_hot_path_imports_without_modal`` — always runs, no infra. A
  subprocess poisons ``import modal``, then imports the entire worker hot path
  and registers every built-in job handler. The regression guard against
  re-coupling: if anything on the hot path imports modal at load, this fails.

* ``test_off_modal_run_records_verdict_without_modal`` — Docker-gated. Stands up
  a throwaway Postgres, then runs the real claim → handler → record-outcome
  machinery (with modal poisoned) to a terminal SUCCESS verdict. Proves the
  control plane runs a job to a verdict off Modal. (The sandbox itself is
  Harbor's job and already non-Modal, spec §3.2, so it is stubbed here.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parents[1] / "src")
_PROOF = str(Path(__file__).resolve().parent / "_offmodal_proof.py")

HOT_PATH_MODULES = [
    "oddish.workers.queue.worker",
    "oddish.workers.queue.queue_manager",
    "oddish.workers.queue.worker_job_single_job",
    "oddish.dispatch.cycle",
    "oddish.dispatch.backends.inprocess",
    "oddish.dispatch.backends.docker",
]


def test_worker_hot_path_imports_without_modal() -> None:
    imports = "\n".join(f"import {m}" for m in HOT_PATH_MODULES)
    code = (
        f"import sys; sys.path.insert(0, {_SRC!r})\n"
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def _guard(name, *a, **k):\n"
        "    if name == 'modal' or name.startswith('modal.'):\n"
        "        raise AssertionError('modal imported on worker hot path: ' + name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _guard\n"
        f"{imports}\n"
        "from oddish.workers.jobs import ensure_builtin_handlers_registered\n"
        "ensure_builtin_handlers_registered()\n"
        "assert 'modal' not in sys.modules, 'modal leaked into sys.modules'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except Exception:
        return False


def _wait_for_postgres(container: str, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", "oddish"],
            capture_output=True,
        )
        if ready.returncode == 0:
            return
        time.sleep(1.0)
    raise RuntimeError("postgres did not become ready in time")


@pytest.mark.skipif(not _docker_available(), reason="requires a running Docker daemon")
def test_off_modal_run_records_verdict_without_modal() -> None:
    container = f"oddish-portability-pg-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", container,
            "-e", "POSTGRES_USER=oddish",
            "-e", "POSTGRES_PASSWORD=oddish",
            "-e", "POSTGRES_DB=oddish",
            "-p", "127.0.0.1::5432",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    try:
        _wait_for_postgres(container)
        port_out = subprocess.run(
            ["docker", "port", container, "5432/tcp"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        # e.g. "127.0.0.1:54321" (possibly multiple lines)
        host_port = port_out.splitlines()[0].rsplit(":", 1)[-1]

        env = dict(os.environ)
        env["ODDISH_DATABASE_URL"] = (
            f"postgresql+asyncpg://oddish:oddish@127.0.0.1:{host_port}/oddish"
        )
        proof = subprocess.run(
            [sys.executable, _PROOF],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proof.returncode == 0, (
            f"proof failed\nstdout:\n{proof.stdout}\nstderr:\n{proof.stderr}"
        )
        assert "STATUS=SUCCESS" in proof.stdout, proof.stdout
        assert "MODAL_IMPORTED=False" in proof.stdout, proof.stdout
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)

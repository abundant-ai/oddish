from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.modal import ModalBackend
from oddish.runtime.registry import get_backend, ordered_backends


def test_get_backend_resolves_modal_and_daytona() -> None:
    assert isinstance(get_backend("modal"), ModalBackend)
    assert isinstance(get_backend("daytona"), DaytonaBackend)


def test_get_backend_is_case_insensitive() -> None:
    assert isinstance(get_backend("MODAL"), ModalBackend)


def test_get_backend_unknown_returns_none() -> None:
    assert get_backend("docker") is None
    assert get_backend("") is None


def test_ordered_backends_is_cheap_first_daytona_then_modal() -> None:
    names = [b.name for b in ordered_backends()]
    assert names == ["daytona", "modal"]


def test_agentenv_e2b_backend_registers_when_configured() -> None:
    code = (
        "from oddish.runtime.registry import ordered_backends;"
        "print(','.join(b.name for b in ordered_backends()))"
    )
    import os
    import subprocess

    env = os.environ.copy()
    env["ODDISH_AGENTENV_API_URL"] = "http://agentenv.test:8000"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split(",") == ["daytona", "modal", "e2b"]

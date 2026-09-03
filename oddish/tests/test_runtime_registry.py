from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.runtime.backends.archil import ArchilBackend
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.modal import ModalBackend
from oddish.runtime.registry import automatic_backends, get_backend, ordered_backends


def test_get_backend_resolves_registered_backends() -> None:
    assert isinstance(get_backend("modal"), ModalBackend)
    assert isinstance(get_backend("daytona"), DaytonaBackend)
    assert isinstance(get_backend("archil"), ArchilBackend)


def test_get_backend_is_case_insensitive() -> None:
    assert isinstance(get_backend("MODAL"), ModalBackend)


def test_get_backend_unknown_returns_none() -> None:
    assert get_backend("docker") is None
    assert get_backend("") is None


def test_ordered_backends_preserves_existing_defaults() -> None:
    names = [b.name for b in ordered_backends()]
    assert names == ["daytona", "modal", "archil"]


def test_automatic_backends_preserves_existing_defaults() -> None:
    names = [b.name for b in automatic_backends()]
    assert names == ["daytona", "modal", "archil"]


def test_thunder_registers_only_when_enabled() -> None:
    code = """
import json
from oddish.runtime.routing import allowed_cloud_environments, default_cloud_environment, select_backend
from oddish.runtime.registry import automatic_backends, ordered_backends
print(json.dumps({
    "registered": [backend.name for backend in ordered_backends()],
    "automatic": [backend.name for backend in automatic_backends()],
    "allowed": sorted(environment.value for environment in allowed_cloud_environments()),
    "gpu_selection": select_backend(requires_gpu=True).name,
    "gpu_default": default_cloud_environment(requires_gpu=True).value,
    "cpu_default": default_cloud_environment().value,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "ODDISH_THUNDER_ENABLED": "true"},
        capture_output=True,
        text=True,
        check=True,
    )

    resolved = json.loads(result.stdout.splitlines()[-1])
    assert "thunder" in resolved["registered"]
    assert "thunder" not in resolved["automatic"]
    assert "thunder" in resolved["allowed"]
    assert resolved["gpu_selection"] == "modal"
    assert resolved["gpu_default"] == "modal"
    assert resolved["cpu_default"] == "daytona"

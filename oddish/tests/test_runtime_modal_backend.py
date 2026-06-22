from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.runtime.backends.modal import ModalBackend


def test_modal_backend_name_matches_environment_value() -> None:
    assert ModalBackend().name == "modal"


def test_modal_capabilities_advertise_gpu_and_private_registry() -> None:
    caps = ModalBackend().capabilities()
    assert caps.gpu is not None
    assert caps.gpu.max_count >= 1
    assert caps.private_registry_pull is True
    assert caps.cold_start == "minutes"
    assert caps.memory_snapshot_fork is True


def test_modal_harbor_env_kwargs_is_passthrough() -> None:
    base = {"agent_tools_image": "ghcr.io/org/tools:tag", "keep": "value"}
    assert ModalBackend().harbor_env_kwargs(dict(base)) == base

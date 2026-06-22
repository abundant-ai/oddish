from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from oddish.runtime.ports import Capabilities, ExecutionBackend, GpuSupport
from oddish.runtime.backends.fake import FakeBackend
from oddish.runtime.backends.modal import ModalBackend

# Real backends are added to this list as they are implemented.
CONFORMANCE_BACKENDS: list[ExecutionBackend] = [FakeBackend(), ModalBackend()]

_COLD_START = {"instant", "seconds", "minutes"}
_EGRESS = {"deny", "allow", "configurable"}


@pytest.mark.parametrize("backend", CONFORMANCE_BACKENDS, ids=lambda b: b.name)
def test_capabilities_self_consistent(backend: ExecutionBackend) -> None:
    caps = backend.capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.cold_start in _COLD_START
    assert caps.network_egress in _EGRESS
    if caps.gpu is not None:
        assert isinstance(caps.gpu, GpuSupport)
        assert len(caps.gpu.accelerators) > 0
        assert caps.gpu.max_count > 0


@pytest.mark.parametrize("backend", CONFORMANCE_BACKENDS, ids=lambda b: b.name)
def test_harbor_env_kwargs_preserves_caller_values(backend: ExecutionBackend) -> None:
    base = {"agent_tools_image": "ghcr.io/org/tools:tag", "keep": "value"}
    merged = backend.harbor_env_kwargs(dict(base))
    assert isinstance(merged, dict)
    # Caller-supplied kwargs always survive the merge (caller wins).
    for key, value in base.items():
        assert merged[key] == value


@pytest.mark.parametrize("backend", CONFORMANCE_BACKENDS, ids=lambda b: b.name)
def test_name_is_nonempty_str(backend: ExecutionBackend) -> None:
    assert isinstance(backend.name, str) and backend.name

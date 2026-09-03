from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from oddish.runtime.ports import (
    Capabilities,
    ExecutionBackend,
    GpuSupport,
    TpuSupport,
)
from oddish.runtime.backends.archil import ArchilBackend
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.fake import FakeBackend
from oddish.runtime.backends.gke import GkeBackend
from oddish.runtime.backends.modal import ModalBackend
from oddish.runtime.backends.thunder import ThunderBackend

# Real backends are added to this list as they are implemented.
CONFORMANCE_BACKENDS: list[ExecutionBackend] = [
    FakeBackend(),
    ModalBackend(),
    DaytonaBackend(),
    ArchilBackend(),
    GkeBackend(),
    ThunderBackend(),
]

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
    if caps.tpu is not None:
        assert isinstance(caps.tpu, TpuSupport)
        assert len(caps.tpu.types) > 0
        assert caps.tpu.max_chips_per_host > 0


@pytest.mark.parametrize("backend", CONFORMANCE_BACKENDS, ids=lambda b: b.name)
def test_harbor_env_kwargs_preserves_caller_values(backend: ExecutionBackend) -> None:
    base = {"region": "us-east", "keep": "value"}
    merged = backend.harbor_env_kwargs(dict(base))
    assert isinstance(merged, dict)
    # Caller-supplied kwargs always survive the merge (caller wins).
    for key, value in base.items():
        assert merged[key] == value


@pytest.mark.parametrize("backend", CONFORMANCE_BACKENDS, ids=lambda b: b.name)
def test_name_is_nonempty_str(backend: ExecutionBackend) -> None:
    assert isinstance(backend.name, str) and backend.name


def test_archil_capabilities_match_current_harbor_support() -> None:
    caps = ArchilBackend().capabilities()
    assert caps.gpu is None
    assert caps.private_registry_pull is False
    assert caps.network_egress == "allow"
    assert caps.persistent_volumes is False
    assert caps.streaming_logs is False


def test_archil_env_kwargs_enable_safe_pause_by_default() -> None:
    assert ArchilBackend().harbor_env_kwargs({})["pause_http_proxy"] is True


def test_archil_env_kwargs_allow_explicit_proxy_override() -> None:
    assert (
        ArchilBackend().harbor_env_kwargs({"pause_http_proxy": False})[
            "pause_http_proxy"
        ]
        is False
    )

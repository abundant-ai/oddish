"""A TPU-requesting trial that lands on a TPU-less backend must fail fast with
an actionable message, not run without the accelerator and fail its own device
asserts minutes later.
"""

from __future__ import annotations

import pytest
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import TpuSpec

from oddish.runtime.backends.gke import GkeBackend
from oddish.workers.harbor.runner import _assert_tpu_backend


class _TpulessCapabilities:
    tpu = None


class _TpulessBackend:
    def capabilities(self) -> _TpulessCapabilities:
        return _TpulessCapabilities()


def test_tpu_request_on_backend_without_tpu_raises_actionable_error():
    with pytest.raises(RuntimeError, match="environment=gke"):
        _assert_tpu_backend(
            EnvironmentType.MODAL,
            _TpulessBackend(),
            TpuSpec(type="v5e", topology="2x2"),
        )


def test_tpu_request_on_unregistered_backend_raises():
    with pytest.raises(RuntimeError, match="environment=gke"):
        _assert_tpu_backend(
            EnvironmentType.DAYTONA, None, TpuSpec(type="v6e", topology="2x2")
        )


def test_tpu_request_on_gke_backend_passes():
    _assert_tpu_backend(
        EnvironmentType.GKE, GkeBackend(), TpuSpec(type="v5e", topology="2x2")
    )


def test_no_tpu_request_never_raises():
    _assert_tpu_backend(EnvironmentType.MODAL, _TpulessBackend(), None)
    _assert_tpu_backend(EnvironmentType.MODAL, None, None)

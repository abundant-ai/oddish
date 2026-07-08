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


def test_gpu_and_tpu_overrides_rejected_at_schema():
    # The CLI rejects GPU+TPU together pre-submit; the schema guard gives raw
    # API submissions the same loud 422 instead of a runtime accelerator error.
    from pydantic import ValidationError

    from oddish.schemas import AgentModelPair, HarborConfig, TaskSweepSubmission

    with pytest.raises(ValidationError, match="GPU and TPU"):
        TaskSweepSubmission(
            task_id="t1",
            configs=[AgentModelPair(agent="oracle")],
            harbor=HarborConfig.model_validate(
                {
                    "environment": {
                        "override_gpus": 1,
                        "override_tpu": {"type": "v5e", "topology": "2x2"},
                    }
                }
            ),
        )


def test_ephemeral_path_hits_tpu_gate_before_dispatch(monkeypatch):
    # The gate must fire BEFORE the ephemeral early-return, or an out-of-process
    # trial with override_tpu on a TPU-less backend skips the fast-fail.
    import asyncio
    from pathlib import Path

    import oddish.workers.harbor.ephemeral as ephemeral
    from oddish.workers.harbor.runner import run_harbor_trial_async

    async def _must_not_run(**kwargs):
        raise AssertionError("ephemeral dispatch reached despite TPU gate")

    monkeypatch.setattr(ephemeral, "run_ephemeral_harbor_trial", _must_not_run)

    with pytest.raises(RuntimeError, match="environment=gke"):
        asyncio.run(
            run_harbor_trial_async(
                task_path=Path("/nonexistent"),
                agent="oracle",
                jobs_dir=Path("/tmp"),
                environment=EnvironmentType.MODAL,
                harbor_config={
                    "variant_id": "ephemeral",
                    "environment": {
                        "override_tpu": {"type": "v5e", "topology": "2x2"}
                    },
                },
            )
        )

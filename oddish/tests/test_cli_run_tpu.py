"""TPU detection and routing in `oddish run`.

Mirrors the GPU path: a task.toml declaring ``[environment.tpu]`` must route to
the TPU-capable backend (GKE) via capability negotiation, while CPU and GPU
tasks keep their existing Daytona/Modal destinations even when GKE is available.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType  # noqa: E402

# oddish.cli re-exports a `run` *command* that shadows the submodule, so pull
# the actual module out of sys.modules via importlib.
run = importlib.import_module("oddish.cli.run")  # noqa: E402


def _patch_env(monkeypatch, *, gpus=None, tpu=None):
    monkeypatch.setattr(
        run,
        "HarborTaskConfig",
        SimpleNamespace(
            model_validate_toml=lambda _text: SimpleNamespace(
                environment=SimpleNamespace(gpus=gpus, tpu=tpu)
            )
        ),
    )


def _stub_registry_with_gke(monkeypatch):
    import oddish.runtime.routing as routing
    from oddish.runtime.backends.daytona import DaytonaBackend
    from oddish.runtime.backends.gke import GkeBackend
    from oddish.runtime.backends.modal import ModalBackend

    monkeypatch.setattr(
        routing,
        "ordered_backends",
        lambda: [DaytonaBackend(), ModalBackend(), GkeBackend()],
    )


def _stub_registry_without_gke(monkeypatch):
    # A laptop submitting to Oddish Cloud has no ODDISH_GKE_CLUSTER_NAME, so the
    # import-time registry never registered GKE -- only Daytona and Modal.
    import oddish.runtime.routing as routing
    from oddish.runtime.backends.daytona import DaytonaBackend
    from oddish.runtime.backends.modal import ModalBackend

    monkeypatch.setattr(
        routing,
        "ordered_backends",
        lambda: [DaytonaBackend(), ModalBackend()],
    )


def test_tpu_present_is_true(tmp_path, monkeypatch):
    (tmp_path / "task.toml").write_text("x")
    _patch_env(monkeypatch, tpu=SimpleNamespace(type="v6e", topology="2x2"))
    assert run._task_config_requests_tpu(tmp_path) is True


def test_tpu_absent_is_false(tmp_path, monkeypatch):
    (tmp_path / "task.toml").write_text("x")
    _patch_env(monkeypatch, tpu=None)
    assert run._task_config_requests_tpu(tmp_path) is False


def test_missing_task_toml_is_false(tmp_path):
    # No task.toml -> parse raises -> guarded to False (mirrors the GPU path).
    assert run._task_config_requests_tpu(tmp_path) is False


def test_tpu_task_default_env_is_gke(tmp_path, monkeypatch):
    (tmp_path / "task.toml").write_text("x")
    _patch_env(monkeypatch, gpus=None, tpu=SimpleNamespace(type="v6e"))
    _stub_registry_with_gke(monkeypatch)
    assert (
        run._default_cloud_environment_for_task(tmp_path, override_gpus=None)
        == EnvironmentType.GKE
    )


def test_tpu_task_routes_to_gke_without_local_gke_registration(tmp_path, monkeypatch):
    # A cloud TPU submission must resolve to GKE even when the local registry
    # lacks GKE (a laptop with no cluster config). The hosted deployment runs
    # GKE and validates against its own cloud policy; the client must not crash
    # with NoEligibleBackendError before the sweep is ever sent.
    (tmp_path / "task.toml").write_text("x")
    _patch_env(monkeypatch, gpus=None, tpu=SimpleNamespace(type="v6e"))
    _stub_registry_without_gke(monkeypatch)
    assert (
        run._default_cloud_environment_for_task(tmp_path, override_gpus=None)
        == EnvironmentType.GKE
    )


def test_cpu_task_stays_daytona_even_with_gke_available(tmp_path, monkeypatch):
    (tmp_path / "task.toml").write_text("x")
    _patch_env(monkeypatch, gpus=0, tpu=None)
    _stub_registry_with_gke(monkeypatch)
    assert (
        run._default_cloud_environment_for_task(tmp_path, override_gpus=None)
        == EnvironmentType.DAYTONA
    )


def test_gpu_and_tpu_task_raises_clear_error(tmp_path, monkeypatch):
    # A task requesting both GPU and TPU cannot be satisfied by any single
    # backend. It must fail with a clear, actionable message naming the
    # conflict -- not the opaque NoEligibleBackendError from negotiation.
    from oddish.runtime.routing import NoEligibleBackendError

    (tmp_path / "task.toml").write_text("x")
    _patch_env(monkeypatch, gpus=2, tpu=SimpleNamespace(type="v6e"))
    _stub_registry_with_gke(monkeypatch)
    with pytest.raises(typer.BadParameter) as excinfo:
        run._default_cloud_environment_for_task(tmp_path, override_gpus=None)
    assert not isinstance(excinfo.value, NoEligibleBackendError)
    message = str(excinfo.value)
    assert "GPU" in message and "TPU" in message


def test_override_gpus_on_tpu_task_raises_clear_error(tmp_path, monkeypatch):
    # The conflict can also arise from --override-gpus on a TPU task.
    (tmp_path / "task.toml").write_text("x")
    _patch_env(monkeypatch, gpus=0, tpu=SimpleNamespace(type="v6e"))
    _stub_registry_with_gke(monkeypatch)
    with pytest.raises(typer.BadParameter):
        run._default_cloud_environment_for_task(tmp_path, override_gpus=4)


def test_gpu_task_stays_modal_even_with_gke_available(tmp_path, monkeypatch):
    (tmp_path / "task.toml").write_text("x")
    _patch_env(monkeypatch, gpus=2, tpu=None)
    _stub_registry_with_gke(monkeypatch)
    assert (
        run._default_cloud_environment_for_task(tmp_path, override_gpus=None)
        == EnvironmentType.MODAL
    )


def test_gke_is_hosted_passthrough_environment():
    # --env gke must survive the hosted-API coercion that forces unknown envs
    # to Modal, alongside the existing Modal/Daytona passthroughs.
    assert EnvironmentType.GKE in run._HOSTED_PASSTHROUGH_ENVIRONMENTS
    assert EnvironmentType.MODAL in run._HOSTED_PASSTHROUGH_ENVIRONMENTS
    assert EnvironmentType.DAYTONA in run._HOSTED_PASSTHROUGH_ENVIRONMENTS

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.runtime.backends.gke import GkeBackend  # noqa: E402
from oddish.runtime.ports import TpuSupport  # noqa: E402


def _registered_backend_names(*, cluster: str | None) -> list[str]:
    """Import the registry in a fresh interpreter with a given GKE cluster
    setting so the module-load registration condition is exercised cleanly."""
    code = (
        "from oddish.runtime.registry import ordered_backends;"
        "print(','.join(b.name for b in ordered_backends()))"
    )
    env = {k: v for k, v in os.environ.items() if k != "ODDISH_GKE_CLUSTER_NAME"}
    if cluster is not None:
        env["ODDISH_GKE_CLUSTER_NAME"] = cluster
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().split(",")


def _install_fake_gke_auth(monkeypatch, api: MagicMock) -> MagicMock:
    """Inject a stand-in ``harbor.environments.gke_auth`` (absent from the
    currently-pinned harbor) whose ``build_core_api`` returns *api*."""
    build_core_api = MagicMock(return_value=api)
    module = types.ModuleType("harbor.environments.gke_auth")
    module.build_core_api = build_core_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "harbor.environments.gke_auth", module)
    return build_core_api


def test_gke_backend_name_matches_environment_value() -> None:
    assert GkeBackend().name == "gke"


def test_gke_backend_declares_registered_harbor_variant() -> None:
    # Decoupling completeness: the backend's variant id must resolve to a
    # registered blessed variant that points at harbor-gke, so a GKE trial
    # dispatches onto the GKE-enabled image (never the lean default).
    from oddish.core.harbor_source import HARBOR_VARIANTS

    assert GkeBackend.harbor_variant_id in HARBOR_VARIANTS
    assert (
        HARBOR_VARIANTS[GkeBackend.harbor_variant_id].source
        == "https://github.com/abundant-ai/harbor-gke"
    )


def test_gke_capabilities_are_tpu_only() -> None:
    caps = GkeBackend().capabilities()
    assert caps.gpu is None
    assert caps.tpu == TpuSupport(types=("v5e", "v6e"), max_chips_per_host=8)
    assert caps.private_registry_pull is False
    assert caps.network_egress == "allow"
    assert caps.persistent_volumes is False
    assert caps.streaming_logs is False
    assert caps.memory_snapshot_fork is False
    assert caps.cold_start == "minutes"


def test_gke_env_kwargs_inject_settings_defaults() -> None:
    merged = GkeBackend().harbor_env_kwargs({})
    assert merged["cluster_name"] == settings.gke_cluster_name
    assert merged["region"] == settings.gke_region
    assert merged["project_id"] == settings.gke_project_id
    assert merged["namespace"] == settings.gke_namespace
    assert merged["registry_location"] == settings.gke_registry_location
    assert merged["registry_name"] == settings.gke_registry_name
    assert merged["flex_start"] == settings.gke_flex_start
    assert merged["auto_build_missing_image"] == settings.gke_auto_build_missing_image
    assert merged["auto_provision_cluster"] == settings.gke_auto_provision_cluster
    assert merged["pod_ready_timeout_sec"] == settings.gke_pod_ready_timeout_sec


def test_gke_env_kwargs_caller_overrides_win() -> None:
    merged = GkeBackend().harbor_env_kwargs({"namespace": "custom-ns", "extra": "x"})
    assert merged["namespace"] == "custom-ns"
    assert merged["extra"] == "x"


def test_gke_env_kwargs_require_prebuilt_image_defaults_true() -> None:
    # The hosted GKE path must NOT fall back to a trial-time gcloud Cloud Build
    # on a task-image miss (opaque FileNotFoundError); require a prebuilt image
    # so Harbor raises an actionable error instead.
    merged = GkeBackend().harbor_env_kwargs({})
    assert merged["require_prebuilt_image"] is True


def test_gke_env_kwargs_caller_can_override_require_prebuilt_image() -> None:
    # Caller-wins: a submission that explicitly opts back into Cloud Build keeps
    # its value, same spread as every other GKE default.
    merged = GkeBackend().harbor_env_kwargs({"require_prebuilt_image": False})
    assert merged["require_prebuilt_image"] is False


def test_gke_teardown_parses_handle_and_deletes_pod(monkeypatch) -> None:
    api = MagicMock()
    build_core_api = _install_fake_gke_auth(monkeypatch, api)

    result = asyncio.run(GkeBackend().teardown("oddish-trials/pod-abc"))

    assert result is True
    build_core_api.assert_called_once_with(
        settings.gke_cluster_name, settings.gke_region, settings.gke_project_id
    )
    api.delete_namespaced_pod.assert_called_once_with(
        name="pod-abc", namespace="oddish-trials", grace_period_seconds=0
    )


def test_gke_teardown_returns_false_on_malformed_handle() -> None:
    # No "namespace/pod" separator -> the unpack raises, teardown swallows it.
    assert asyncio.run(GkeBackend().teardown("no-separator")) is False


def test_gke_teardown_returns_false_when_delete_raises(monkeypatch) -> None:
    api = MagicMock()
    api.delete_namespaced_pod.side_effect = RuntimeError("boom")
    _install_fake_gke_auth(monkeypatch, api)

    assert asyncio.run(GkeBackend().teardown("oddish-trials/pod-abc")) is False


def test_gke_teardown_never_raises_when_backend_unconfigured() -> None:
    # No GKE cluster/region/project is configured, so building the real
    # gke_auth API client fails; teardown must swallow it and report failure
    # rather than propagating.
    assert asyncio.run(GkeBackend().teardown("oddish-trials/pod-abc")) is False


def test_gke_capture_diagnostics_is_noop(tmp_path) -> None:
    with GkeBackend().capture_diagnostics(tmp_path) as result:
        assert result is None


def test_gke_absent_from_registry_without_cluster_config() -> None:
    assert "gke" not in _registered_backend_names(cluster=None)


def test_gke_registered_after_modal_when_cluster_configured() -> None:
    names = _registered_backend_names(cluster="oddish-tpu")
    assert "gke" in names
    # Never a cheap-first default: GKE follows Modal in the negotiation order.
    assert names.index("modal") < names.index("gke")

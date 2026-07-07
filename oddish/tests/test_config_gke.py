from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import Settings  # noqa: E402

_GKE_ENV = (
    "ODDISH_GKE_CLUSTER_NAME",
    "ODDISH_GKE_REGION",
    "ODDISH_GKE_PROJECT_ID",
    "ODDISH_GKE_NAMESPACE",
    "ODDISH_GKE_REGISTRY_LOCATION",
    "ODDISH_GKE_REGISTRY_NAME",
    "ODDISH_GKE_FLEX_START",
    "ODDISH_GKE_POD_READY_TIMEOUT_SEC",
)


def _clear(monkeypatch) -> None:
    for name in _GKE_ENV:
        monkeypatch.delenv(name, raising=False)


def test_gke_settings_defaults(monkeypatch) -> None:
    _clear(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.gke_cluster_name is None
    assert settings.gke_region is None
    assert settings.gke_project_id is None
    assert settings.gke_namespace == "oddish-trials"
    assert settings.gke_registry_location is None
    assert settings.gke_registry_name is None
    assert settings.gke_flex_start is True
    assert settings.gke_pod_ready_timeout_sec == 3600


def test_gke_settings_read_from_env(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("ODDISH_GKE_CLUSTER_NAME", "oddish-tpu")
    monkeypatch.setenv("ODDISH_GKE_REGION", "us-east5")
    monkeypatch.setenv("ODDISH_GKE_PROJECT_ID", "my-project")
    monkeypatch.setenv("ODDISH_GKE_NAMESPACE", "custom-ns")
    monkeypatch.setenv("ODDISH_GKE_REGISTRY_LOCATION", "us-east5")
    monkeypatch.setenv("ODDISH_GKE_REGISTRY_NAME", "oddish-envs")
    monkeypatch.setenv("ODDISH_GKE_FLEX_START", "false")
    monkeypatch.setenv("ODDISH_GKE_POD_READY_TIMEOUT_SEC", "1800")
    settings = Settings(_env_file=None)
    assert settings.gke_cluster_name == "oddish-tpu"
    assert settings.gke_region == "us-east5"
    assert settings.gke_project_id == "my-project"
    assert settings.gke_namespace == "custom-ns"
    assert settings.gke_registry_location == "us-east5"
    assert settings.gke_registry_name == "oddish-envs"
    assert settings.gke_flex_start is False
    assert settings.gke_pod_ready_timeout_sec == 1800

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

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
    "ODDISH_GKE_SPOT",
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


def test_gke_spot_defaults_off(monkeypatch) -> None:
    """Spot must be opt-in: it trades preemptibility for reach."""
    _clear(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.gke_spot is False


def test_gke_spot_reads_the_environment(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("ODDISH_GKE_SPOT", "true")
    monkeypatch.setenv("ODDISH_GKE_FLEX_START", "false")
    settings = Settings(_env_file=None)
    assert settings.gke_spot is True
    assert settings.gke_flex_start is False


def test_gke_both_provisioning_modes_true_is_rejected(monkeypatch) -> None:
    """The deployment-level trap: gke_flex_start defaults to True.

    An operator who sets only ODDISH_GKE_SPOT=true leaves both true, and
    per-submission normalization cannot help -- there is no caller kwarg to
    disambiguate. Every GKE trial would then hit Harbor's both-true rejection
    and retry to exhaustion. Fail once, at config load, instead.
    """
    _clear(monkeypatch)
    monkeypatch.setenv("ODDISH_GKE_SPOT", "true")
    # ODDISH_GKE_FLEX_START deliberately unset, so it keeps its True default.
    with pytest.raises(ValidationError, match="cannot both be true"):
        Settings(_env_file=None)


def test_gke_spot_alone_is_fine_when_flex_is_disabled(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("ODDISH_GKE_SPOT", "true")
    monkeypatch.setenv("ODDISH_GKE_FLEX_START", "false")
    settings = Settings(_env_file=None)
    assert settings.gke_spot is True
    assert settings.gke_flex_start is False

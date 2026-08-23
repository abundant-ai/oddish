from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import GKE_PROVISIONING_MODES, Settings  # noqa: E402

_GKE_ENV = (
    "ODDISH_GKE_CLUSTER_NAME",
    "ODDISH_GKE_REGION",
    "ODDISH_GKE_PROJECT_ID",
    "ODDISH_GKE_NAMESPACE",
    "ODDISH_GKE_REGISTRY_LOCATION",
    "ODDISH_GKE_REGISTRY_NAME",
    "ODDISH_GKE_PROVISIONING_MODE",
    "ODDISH_GKE_POD_READY_TIMEOUT_SEC",
    "ODDISH_GKE_SPOT",
    "ODDISH_GKE_FLEX_START",
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
    assert settings.gke_provisioning_mode == "flex-start"
    assert settings.gke_pod_ready_timeout_sec == 3600


def test_gke_settings_read_from_env(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("ODDISH_GKE_CLUSTER_NAME", "oddish-tpu")
    monkeypatch.setenv("ODDISH_GKE_REGION", "us-east5")
    monkeypatch.setenv("ODDISH_GKE_PROJECT_ID", "my-project")
    monkeypatch.setenv("ODDISH_GKE_NAMESPACE", "custom-ns")
    monkeypatch.setenv("ODDISH_GKE_REGISTRY_LOCATION", "us-east5")
    monkeypatch.setenv("ODDISH_GKE_REGISTRY_NAME", "oddish-envs")
    monkeypatch.setenv("ODDISH_GKE_PROVISIONING_MODE", "on-demand")
    monkeypatch.setenv("ODDISH_GKE_POD_READY_TIMEOUT_SEC", "1800")
    settings = Settings(_env_file=None)
    assert settings.gke_cluster_name == "oddish-tpu"
    assert settings.gke_region == "us-east5"
    assert settings.gke_project_id == "my-project"
    assert settings.gke_namespace == "custom-ns"
    assert settings.gke_registry_location == "us-east5"
    assert settings.gke_registry_name == "oddish-envs"
    assert settings.gke_provisioning_mode == "on-demand"
    assert settings.gke_pod_ready_timeout_sec == 1800


@pytest.mark.parametrize("mode", GKE_PROVISIONING_MODES)
def test_gke_provisioning_mode_accepts_every_harbor_mode(monkeypatch, mode) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("ODDISH_GKE_PROVISIONING_MODE", mode)
    assert Settings(_env_file=None).gke_provisioning_mode == mode


def test_gke_provisioning_mode_rejects_a_value_harbor_does_not_accept(
    monkeypatch,
) -> None:
    """A typo must stop the deploy, not every trial the deploy then runs.

    The setting is a free string off the environment, so "flexstart" would
    reach Harbor unread and raise GKEConfigurationError inside every GKE
    environment construction -- once per trial, with nobody watching for it.
    """
    _clear(monkeypatch)
    monkeypatch.setenv("ODDISH_GKE_PROVISIONING_MODE", "flexstart")
    with pytest.raises(ValidationError, match="is not a provisioning mode"):
        Settings(_env_file=None)


def test_gke_provisioning_mode_error_lists_the_valid_values(monkeypatch) -> None:
    # The author has to be told what to type instead; "invalid" alone is not
    # enough when the accepted spelling is hyphenated and non-obvious.
    _clear(monkeypatch)
    monkeypatch.setenv("ODDISH_GKE_PROVISIONING_MODE", "flexstart")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    for mode in GKE_PROVISIONING_MODES:
        assert repr(mode) in str(excinfo.value)


def test_removed_boolean_mode_variables_fail_loudly(monkeypatch) -> None:
    """The removed knobs must not be silently ignored: a deployment still
    exporting one would otherwise fall back to the flex-start default and
    change provisioning modes without a word."""
    monkeypatch.setenv("ODDISH_GKE_SPOT", "true")
    with pytest.raises(ValidationError, match="ODDISH_GKE_SPOT is removed"):
        Settings(_env_file=None)


def test_both_removed_variables_are_named_together(monkeypatch) -> None:
    monkeypatch.setenv("ODDISH_GKE_SPOT", "false")
    monkeypatch.setenv("ODDISH_GKE_FLEX_START", "true")
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None)
    message = str(err.value)
    assert "ODDISH_GKE_SPOT" in message
    assert "ODDISH_GKE_FLEX_START" in message
    assert "ODDISH_GKE_PROVISIONING_MODE" in message


def test_removed_variables_are_rejected_from_dotenv_files(
    tmp_path, monkeypatch
) -> None:
    """The rejection must cover every settings source, not just the process
    environment: a dotenv holder would otherwise fall back to the default
    mode silently."""
    env_file = tmp_path / ".env"
    env_file.write_text("ODDISH_GKE_FLEX_START=true\n")
    with pytest.raises(ValidationError, match="ODDISH_GKE_FLEX_START is removed"):
        Settings(_env_file=str(env_file))

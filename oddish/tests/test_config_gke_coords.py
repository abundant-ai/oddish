"""The baked coordinate snapshot outranks the container environment.

When GKE is enabled the runtime secret injects ODDISH_GKE_* into os.environ
at container init, so env alone cannot tell a deploy's value from the
secret's. The image file can only have come from the deploy, so settings
prefer it and warn, with both values named, when the environment disagrees.
"""

from __future__ import annotations

import json
import logging

import pytest

import oddish.config as config_module
from oddish.config import Settings


@pytest.fixture()
def coords_file(tmp_path, monkeypatch):
    path = tmp_path / "gke_coords.json"

    def _write(payload) -> None:
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))

    monkeypatch.setattr(config_module, "_GKE_COORDS_PATH", path)
    return _write


def test_the_baked_value_beats_the_environment(coords_file, monkeypatch):
    coords_file({"ODDISH_GKE_REGION": "region-from-image"})
    monkeypatch.setenv("ODDISH_GKE_REGION", "region-from-secret")
    assert Settings().gke_region == "region-from-image"


def test_divergence_is_warned_with_both_values_named(
    coords_file, monkeypatch, caplog
):
    coords_file({"ODDISH_GKE_REGION": "region-from-image"})
    monkeypatch.setenv("ODDISH_GKE_REGION", "region-from-secret")
    with caplog.at_level(logging.WARNING):
        Settings()
    hits = [r for r in caplog.records if "ODDISH_GKE_REGION" in r.getMessage()]
    assert hits, "no warning named the diverging key"
    message = hits[0].getMessage()
    assert "region-from-image" in message and "region-from-secret" in message


def test_agreement_warns_nothing(coords_file, monkeypatch, caplog):
    coords_file({"ODDISH_GKE_REGION": "same"})
    monkeypatch.setenv("ODDISH_GKE_REGION", "same")
    with caplog.at_level(logging.WARNING):
        Settings()
    assert not [r for r in caplog.records if "ODDISH_GKE_REGION" in r.getMessage()]


def test_no_file_leaves_the_environment_in_charge(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config_module, "_GKE_COORDS_PATH", tmp_path / "does-not-exist.json"
    )
    monkeypatch.setenv("ODDISH_GKE_REGION", "region-from-env")
    assert Settings().gke_region == "region-from-env"


def test_a_garbage_file_is_ignored_not_fatal(coords_file, monkeypatch):
    coords_file("not json at all {")
    monkeypatch.setenv("ODDISH_GKE_REGION", "region-from-env")
    assert Settings().gke_region == "region-from-env"


def test_an_explicit_constructor_argument_still_wins(coords_file):
    """Tests and callers that pass values directly must stay authoritative."""
    coords_file({"ODDISH_GKE_REGION": "region-from-image"})
    assert Settings(gke_region="explicit").gke_region == "explicit"


def test_baked_values_flow_through_field_coercion(coords_file):
    """The snapshot holds strings; a bool field must still come out a bool.

    This is why the override is a settings SOURCE rather than a validator
    doing its own casting."""
    coords_file({"ODDISH_GKE_AUTO_PROVISION_CLUSTER": "false"})
    assert Settings().gke_auto_provision_cluster is False


def test_non_gke_keys_in_the_file_change_nothing(coords_file, monkeypatch):
    coords_file({"ODDISH_API_URL": "https://smuggled.example"})
    monkeypatch.delenv("ODDISH_API_URL", raising=False)
    before = Settings().api_url if hasattr(Settings(), "api_url") else None
    assert before is None or "smuggled" not in str(before)


def test_the_identity_key_specifically_cannot_be_stolen_by_a_secret(
    coords_file, monkeypatch
):
    """The cluster name is the identity every deletion authorizes against.
    With the derived name baked, a runtime secret that injects
    ODDISH_GKE_CLUSTER_NAME loses, and the theft is warned, not silent."""
    coords_file({"ODDISH_GKE_CLUSTER_NAME": "dep-trials"})
    monkeypatch.setenv("ODDISH_GKE_CLUSTER_NAME", "attacker-trials")
    monkeypatch.setenv("ODDISH_GKE_PROJECT_ID", "p")
    assert Settings().gke_cluster_name == "dep-trials"

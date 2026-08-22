"""Deploy-time GKE coordinates are baked into the image as a file.

The ODDISH_GKE_* coordinates travel two ways today: baked into the image env
(the ENV_VARS filter) and inside the ``oddish-gcp`` runtime secret. A runtime
secret injects into ``os.environ`` at container init, so by the time settings
read env the secret has already beaten the deploy's env block -- silently. A
file baked into the image is the one channel a secret cannot write, so the
snapshot written here is what lets the worker prefer the deploy's values and
say so when they diverge.
"""

from __future__ import annotations

import modal_app


def test_snapshot_keeps_only_gke_keys_and_process_env_wins():
    snapshot = modal_app._gke_coords_snapshot(
        {"ODDISH_GKE_REGION": "region-from-env", "UNRELATED": "x"},
        {"ODDISH_GKE_REGION": "region-from-dotenv", "ODDISH_GKE_PROJECT_ID": "p"},
    )
    assert snapshot == {
        "ODDISH_GKE_REGION": "region-from-env",
        "ODDISH_GKE_PROJECT_ID": "p",
        # The project id resolves an identity, so the derived name is baked
        # too -- see test_a_derived_cluster_name_is_baked_when_the_deploy_resolves_one.
        "ODDISH_GKE_CLUSTER_NAME": f"{modal_app.MODAL_APP_NAME}-trials",
    }


def test_snapshot_is_empty_when_nothing_is_configured():
    assert modal_app._gke_coords_snapshot({"OTHER": "1"}, {}) == {}


def test_coords_file_lives_beside_the_secret_plan():
    """Same immutable-image channel, same directory, so one mkdir covers both."""
    import os

    assert os.path.dirname(modal_app._GKE_COORDS_FILE) == os.path.dirname(
        modal_app._GKE_PLAN_FILE
    )


def test_module_snapshot_matches_the_baked_env_exactly():
    """Drift guard: the file and the image env must describe the same deploy.

    ENV_VARS bakes every ODDISH_GKE_* key from {dotenv, env}; the snapshot is
    written from the same inputs. One deliberate addition is allowed: the
    EFFECTIVE cluster name, baked even when the deploy derives it, because it
    is the identity every deletion authorizes against.
    """
    baked = {
        k: v for k, v in modal_app.ENV_VARS.items() if k.startswith("ODDISH_GKE_")
    }
    snapshot = dict(modal_app.GKE_COORDS_SNAPSHOT)
    extra = set(snapshot) - set(baked)
    assert extra <= {modal_app._GKE_CLUSTER_ENV}, (
        f"snapshot carries keys the baked env does not: {sorted(extra)}"
    )
    if modal_app._GKE_CLUSTER_ENV in extra:
        import os

        assert snapshot.pop(modal_app._GKE_CLUSTER_ENV) == modal_app._effective_gke_cluster_name(
            os.environ, modal_app.LOCAL_DOTENV_VARS
        )
    assert snapshot == {k: v for k, v in baked.items()}


def test_a_derived_cluster_name_is_baked_when_the_deploy_resolves_one():
    """The recommended path leaves the name unset and sets the project id.
    Without this bake, that exact path leaves the identity key absent from
    the file, and a runtime secret can inject it unopposed."""
    snapshot = modal_app._gke_coords_snapshot(
        {"ODDISH_GKE_PROJECT_ID": "p"}, {}
    )
    assert (
        snapshot["ODDISH_GKE_CLUSTER_NAME"]
        == f"{modal_app.MODAL_APP_NAME}-trials"
    )


def test_an_explicit_cluster_name_is_kept_not_rederived():
    snapshot = modal_app._gke_coords_snapshot(
        {"ODDISH_GKE_PROJECT_ID": "p", "ODDISH_GKE_CLUSTER_NAME": "explicit"}, {}
    )
    assert snapshot["ODDISH_GKE_CLUSTER_NAME"] == "explicit"


def test_no_identity_is_baked_when_the_deploy_resolves_none():
    """GKE-less deploys, and the flow where the credential secret carries
    the coordinates by design, must not have a name invented for them --
    baking one would register the backend, or fight the designed channel."""
    for deploy_env in (
        {"OTHER": "x"},
        # The enabled flag alone: coordinates ride the credential secret by
        # design there, so no identity resolves at deploy time.
        {"ODDISH_GKE_ENABLED": "true"},
        # A GKE knob that is not an identity source must not trigger a bake.
        {"ODDISH_GKE_REGION": "r"},
    ):
        snapshot = modal_app._gke_coords_snapshot(deploy_env, {})
        assert "ODDISH_GKE_CLUSTER_NAME" not in snapshot, deploy_env


def test_an_empty_name_in_the_deploy_env_does_not_block_the_bake():
    """The resolver treats an empty name as unset, so the bake must too --
    key presence alone would ship an empty identity for the runtime to
    re-derive from a mutable app name."""
    snapshot = modal_app._gke_coords_snapshot(
        {"ODDISH_GKE_PROJECT_ID": "p", "ODDISH_GKE_CLUSTER_NAME": ""}, {}
    )
    assert (
        snapshot["ODDISH_GKE_CLUSTER_NAME"]
        == f"{modal_app.MODAL_APP_NAME}-trials"
    )


def test_an_empty_name_with_no_identity_is_dropped_not_shipped():
    snapshot = modal_app._gke_coords_snapshot(
        {"ODDISH_GKE_CLUSTER_NAME": ""}, {}
    )
    assert "ODDISH_GKE_CLUSTER_NAME" not in snapshot

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
    written from the same inputs. If either filter changes without the other,
    the worker would warn about (or silently miss) divergence that is really
    just the two bake sites disagreeing.
    """
    baked = {
        k: v for k, v in modal_app.ENV_VARS.items() if k.startswith("ODDISH_GKE_")
    }
    assert modal_app.GKE_COORDS_SNAPSHOT == baked

"""Deploy-time gate that routes GKE config through the ``oddish-gke-config``
runtime secret.

Single-tenant GKE enablement carries ODDISH_GKE_* (cluster/registry coordinates
plus the org allowlist) in a Modal *runtime* secret instead of baking it from
``backend/.env``. The secret is referenced only when a deploy-time flag
(ODDISH_GKE_ENABLED) says so, so an environment without the secret still
deploys. These tests pin two contracts:

* ``_gke_config_secret_enabled`` reads the flag from the deploy env / .env.
* ``_gke_runtime_secret_names`` composes the optional GKE secrets so a GKE-less
  deploy references none, and the decision is identical at deploy time and at
  in-container recompute (Modal matches function dependencies by count, so the
  two lists must never diverge).
"""

from __future__ import annotations

import modal_app

GCP = "oddish-gcp"
CONFIG = "oddish-gke-config"


# --- the flag reader -------------------------------------------------------


def test_flag_absent_is_disabled():
    assert modal_app._gke_config_secret_enabled({}, {}) is False


def test_flag_truthy_variants_enable():
    for value in ("1", "true", "TRUE", "yes", "on", "  true  "):
        assert modal_app._gke_config_secret_enabled(
            {"ODDISH_GKE_ENABLED": value}, {}
        ), value


def test_flag_falsy_variants_stay_disabled():
    for value in ("0", "false", "no", "off", ""):
        assert not modal_app._gke_config_secret_enabled(
            {"ODDISH_GKE_ENABLED": value}, {}
        ), value


def test_flag_from_dotenv():
    assert modal_app._gke_config_secret_enabled({}, {"ODDISH_GKE_ENABLED": "true"})


def test_explicit_env_false_overrides_dotenv_true():
    # A process-env value wins over .env, mirroring _effective_gke_cluster_name;
    # an explicit "false" in the deploy env disables even if .env enabled it.
    assert not modal_app._gke_config_secret_enabled(
        {"ODDISH_GKE_ENABLED": "false"}, {"ODDISH_GKE_ENABLED": "true"}
    )


# --- the secret-list composition -------------------------------------------


def test_gke_less_deploy_references_no_gke_secrets():
    assert modal_app._gke_runtime_secret_names({}, {}) == []


def test_legacy_dotenv_config_attaches_only_gcp():
    # backend/.env carries the coordinates (the pre-existing path): GCP creds
    # attach, but no config secret is referenced (config is baked from .env).
    env = {"ODDISH_GKE_PROJECT_ID": "abundant-default"}
    assert modal_app._gke_runtime_secret_names({}, env) == [GCP]


def test_explicit_cluster_name_legacy_attaches_only_gcp():
    env = {"ODDISH_GKE_CLUSTER_NAME": "oddish-trials"}
    assert modal_app._gke_runtime_secret_names({}, env) == [GCP]


def test_runtime_secret_flag_attaches_gcp_and_config():
    # The new path: only the flag is set at deploy (coordinates live in the
    # secret, injected at runtime). Both GCP creds and the config secret attach.
    env = {"ODDISH_GKE_ENABLED": "true"}
    assert modal_app._gke_runtime_secret_names(env, {}) == [GCP, CONFIG]


def test_both_paths_do_not_duplicate_gcp():
    env = {"ODDISH_GKE_ENABLED": "true", "ODDISH_GKE_PROJECT_ID": "abundant-default"}
    assert modal_app._gke_runtime_secret_names(env, {}) == [GCP, CONFIG]


def test_deploy_and_container_recompute_agree_runtime_secret_path():
    """The crux: deploy-time and in-container secret lists must match exactly.

    At deploy only ODDISH_GKE_ENABLED is set (coordinates live in the secret).
    In-container, ``backend/.env`` is gone (empty dotenv) and Modal has baked the
    ODDISH_GKE_* deploy env into the image env -- which, for this path, is just
    the flag. Whether or not the runtime secret has already injected the project
    id into ``os.environ`` by the time the module recomputes the list, the names
    must be identical, or the function crashloops on a dependency-count drift.
    """
    deploy_env = {"ODDISH_GKE_ENABLED": "true"}
    deploy_names = modal_app._gke_runtime_secret_names(deploy_env, {})

    baked = {
        k: v for k, v in deploy_env.items() if k.startswith("ODDISH_GKE_")
    }
    # container recompute, secret not yet injected
    assert modal_app._gke_runtime_secret_names(dict(baked), {}) == deploy_names
    # container recompute, secret already injected the project id
    injected = {**baked, "ODDISH_GKE_PROJECT_ID": "abundant-default"}
    assert modal_app._gke_runtime_secret_names(injected, {}) == deploy_names


def test_deploy_and_container_recompute_agree_legacy_path():
    # Legacy .env path: coordinates are baked into the image env, so the
    # container sees them in os.environ with an empty dotenv. Same names.
    dotenv = {"ODDISH_GKE_PROJECT_ID": "abundant-default"}
    deploy_names = modal_app._gke_runtime_secret_names({}, dotenv)
    baked = {k: v for k, v in dotenv.items() if k.startswith("ODDISH_GKE_")}
    assert modal_app._gke_runtime_secret_names(dict(baked), {}) == deploy_names

"""Deploy-time gate that routes GKE config through the ``oddish-gke-config``
runtime secret.

Single-tenant GKE enablement carries the ODDISH_GKE_* (cluster/registry
coordinates) in a Modal *runtime* secret instead of baking it from
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


# --- the plan is read from immutable image metadata in-container -----------
#
# The recompute inside a worker container must NOT re-derive the plan from
# os.environ, because an unrelated runtime secret could carry ODDISH_GKE_ENABLED
# and flip the list, drifting Modal's dependency count into a hydration
# crashloop. _resolve_gke_secret_plan reads the deploy-time plan baked into the
# image env instead.


def test_plan_deploy_time_derives_from_env(monkeypatch):
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: True)
    plan = modal_app._resolve_gke_secret_plan({"ODDISH_GKE_ENABLED": "true"}, {})
    assert plan == [GCP, CONFIG]


def test_plan_in_container_reads_baked_value_not_env(monkeypatch):
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: False)
    # Baked plan (from a runtime-secret-flag deploy) is authoritative.
    env = {modal_app._GKE_PLAN_ENV: f"{GCP},{CONFIG}"}
    assert modal_app._resolve_gke_secret_plan(env, {}) == [GCP, CONFIG]


def test_plan_in_container_immune_to_secret_injected_flag(monkeypatch):
    # The crux of codex finding 5: a GKE-less deploy baked an EMPTY plan; a
    # runtime secret then injects ODDISH_GKE_ENABLED=true into the container env.
    # The recompute must still yield [] (matching deploy), not attach secrets and
    # crashloop on a dependency-count mismatch.
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: False)
    polluted = {modal_app._GKE_PLAN_ENV: "", "ODDISH_GKE_ENABLED": "true"}
    assert modal_app._resolve_gke_secret_plan(polluted, {}) == []


def test_plan_in_container_empty_when_unset(monkeypatch):
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: False)
    assert modal_app._resolve_gke_secret_plan({}, {}) == []


def test_baked_plan_env_is_in_env_vars():
    # The deploy-time plan must be baked so the container can read it back.
    assert modal_app._GKE_PLAN_ENV in modal_app.ENV_VARS
    # GKE-less test env -> empty plan baked.
    assert modal_app.ENV_VARS[modal_app._GKE_PLAN_ENV] == ""

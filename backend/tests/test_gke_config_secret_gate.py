"""Deploy-time gate that delivers GKE config through the ``oddish-gcp`` secret.

Single-tenant GKE enablement carries the ODDISH_GKE_* cluster/registry
coordinates inside the existing ``oddish-gcp`` runtime secret (alongside the GCP
SA creds), instead of baking them from ``backend/.env``. That secret is attached
only when a deploy-time flag (ODDISH_GKE_ENABLED) says so, so an environment
without the coordinates still deploys. These tests pin two contracts:

* ``_gke_enabled_flag`` reads the flag from the deploy env / .env.
* ``_gke_runtime_secret_names`` composes the GKE secret so a GKE-less deploy
  references none, and the decision is identical at deploy time and at
  in-container recompute (Modal matches function dependencies by count, so the
  two lists must never diverge).
"""

from __future__ import annotations

import modal_app

GCP = "oddish-gcp"


# --- the flag reader -------------------------------------------------------


def test_flag_absent_is_disabled():
    assert modal_app._gke_enabled_flag({}, {}) is False


def test_flag_truthy_variants_enable():
    for value in ("1", "true", "TRUE", "yes", "on", "  true  "):
        assert modal_app._gke_enabled_flag({"ODDISH_GKE_ENABLED": value}, {}), value


def test_flag_falsy_variants_stay_disabled():
    for value in ("0", "false", "no", "off", ""):
        assert not modal_app._gke_enabled_flag({"ODDISH_GKE_ENABLED": value}, {}), value


def test_flag_from_dotenv():
    assert modal_app._gke_enabled_flag({}, {"ODDISH_GKE_ENABLED": "true"})


def test_explicit_env_false_overrides_dotenv_true():
    # A process-env value wins over .env, mirroring _effective_gke_cluster_name;
    # an explicit "false" in the deploy env disables even if .env enabled it.
    assert not modal_app._gke_enabled_flag(
        {"ODDISH_GKE_ENABLED": "false"}, {"ODDISH_GKE_ENABLED": "true"}
    )


# --- the secret-list composition -------------------------------------------


def test_gke_less_deploy_references_no_gke_secrets():
    assert modal_app._gke_runtime_secret_names({}, {}) == []


def test_legacy_dotenv_config_attaches_gcp():
    # backend/.env carries the coordinates (the pre-existing path): oddish-gcp
    # attaches for the SA creds; the coordinates are baked from .env.
    env = {"ODDISH_GKE_PROJECT_ID": "abundant-default"}
    assert modal_app._gke_runtime_secret_names({}, env) == [GCP]


def test_explicit_cluster_name_legacy_attaches_gcp():
    env = {"ODDISH_GKE_CLUSTER_NAME": "oddish-trials"}
    assert modal_app._gke_runtime_secret_names({}, env) == [GCP]


def test_flag_attaches_gcp():
    # The runtime path: only the flag is set at deploy; oddish-gcp (which now also
    # carries the ODDISH_GKE_* coordinates, injected at runtime) is the one secret.
    env = {"ODDISH_GKE_ENABLED": "true"}
    assert modal_app._gke_runtime_secret_names(env, {}) == [GCP]


def test_both_paths_do_not_duplicate_gcp():
    env = {"ODDISH_GKE_ENABLED": "true", "ODDISH_GKE_PROJECT_ID": "abundant-default"}
    assert modal_app._gke_runtime_secret_names(env, {}) == [GCP]


def test_deploy_and_container_recompute_agree_runtime_flag_path():
    """The crux: deploy-time and in-container secret lists must match exactly.

    At deploy only ODDISH_GKE_ENABLED is set (coordinates live in oddish-gcp).
    In-container, ``backend/.env`` is gone (empty dotenv) and Modal has baked the
    ODDISH_GKE_* deploy env into the image env -- which, for this path, is just
    the flag. Whether or not the runtime secret has already injected the project
    id into ``os.environ`` by the time the module recomputes the list, the names
    must be identical, or the function crashloops on a dependency-count drift.
    """
    deploy_env = {"ODDISH_GKE_ENABLED": "true"}
    deploy_names = modal_app._gke_runtime_secret_names(deploy_env, {})

    baked = {k: v for k, v in deploy_env.items() if k.startswith("ODDISH_GKE_")}
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


# --- the plan is read from an immutable image FILE in-container ------------
#
# The recompute inside a worker container must NOT re-derive the plan from
# os.environ, because a runtime secret could carry ODDISH_GKE_* (or even
# ODDISH_GKE_SECRET_PLAN) and flip the list, drifting Modal's dependency count
# into a hydration crashloop. _resolve_gke_secret_plan reads the deploy-time plan
# from a file baked into the image -- a channel a runtime secret cannot override.


def test_plan_deploy_time_derives_from_env(monkeypatch):
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: True)
    plan = modal_app._resolve_gke_secret_plan({"ODDISH_GKE_ENABLED": "true"}, {})
    assert plan == [GCP]


def test_plan_in_container_reads_baked_file(monkeypatch, tmp_path):
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: False)
    plan_file = tmp_path / "plan"
    plan_file.write_text(GCP)
    monkeypatch.setattr(modal_app, "_GKE_PLAN_FILE", str(plan_file))
    # Even a fully polluted env is ignored -- only the file is read.
    assert modal_app._resolve_gke_secret_plan({"ODDISH_GKE_ENABLED": "x"}, {}) == [GCP]


def test_plan_in_container_immune_to_secret_injected_env(monkeypatch, tmp_path):
    # Codex P1: a GKE-less deploy baked an EMPTY plan file; a runtime secret then
    # injects BOTH ODDISH_GKE_ENABLED and ODDISH_GKE_SECRET_PLAN into the
    # container env. The file is authoritative, so the recompute still yields []
    # (matching deploy) -- no dependency-count drift, no crashloop.
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: False)
    plan_file = tmp_path / "plan"
    plan_file.write_text("")  # GKE-less deploy baked an empty plan
    monkeypatch.setattr(modal_app, "_GKE_PLAN_FILE", str(plan_file))
    polluted = {"ODDISH_GKE_ENABLED": "true", "ODDISH_GKE_SECRET_PLAN": GCP}
    assert modal_app._resolve_gke_secret_plan(polluted, {}) == []


def test_plan_in_container_empty_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: False)
    monkeypatch.setattr(modal_app, "_GKE_PLAN_FILE", str(tmp_path / "missing"))
    assert modal_app._resolve_gke_secret_plan({}, {}) == []


def test_plan_not_baked_into_env_vars():
    # The plan is baked as an image file, not an env var, so a runtime secret
    # cannot override it. Ensure the internal key never lands in the image env.
    assert "ODDISH_GKE_SECRET_PLAN" not in modal_app.ENV_VARS
    # GKE-less test env -> empty plan.
    assert modal_app.GKE_SECRET_PLAN == []

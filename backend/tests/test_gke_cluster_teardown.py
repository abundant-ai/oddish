"""Deployment teardown may delete only its own, harbor-managed cluster."""

from worker.gke_cluster_reaper import select_teardown_targets


def _cluster(name, managed, location="loc-a"):
    labels = {"harbor-managed": "true"} if managed else {}
    return (name, labels, f"projects/p/locations/{location}/clusters/{name}")


def test_the_deployments_managed_cluster_is_selected():
    targets = select_teardown_targets([_cluster("dep-trials", True)], "dep-trials")
    assert targets == ["projects/p/locations/loc-a/clusters/dep-trials"]


def test_every_location_is_covered():
    """A region override on a submission can provision the cluster somewhere
    the deployment settings never name, so teardown searches all locations."""
    clusters = [
        _cluster("dep-trials", True, "loc-a"),
        _cluster("dep-trials", True, "loc-b"),
    ]
    assert len(select_teardown_targets(clusters, "dep-trials")) == 2


def test_an_unmanaged_cluster_is_never_touched():
    assert select_teardown_targets([_cluster("dep-trials", False)], "dep-trials") == []


def test_another_deployments_cluster_is_never_touched():
    assert select_teardown_targets([_cluster("other-trials", True)], "dep-trials") == []


def test_a_managed_label_with_the_wrong_value_does_not_count():
    cluster = ("dep-trials", {"harbor-managed": "yes"}, "projects/p/locations/l/clusters/dep-trials")
    assert select_teardown_targets([cluster], "dep-trials") == []


def test_teardown_only_owns_the_app_derived_name():
    from worker.gke_cluster_reaper import teardown_owns_cluster

    assert teardown_owns_cluster("dep-trials", "dep") is True
    # An explicitly configured cluster -- shared, staging, someone else's
    # deployment -- is a cluster this app was POINTED AT, not one it owns.
    assert teardown_owns_cluster("other-trials", "dep") is False
    assert teardown_owns_cluster("shared-cluster", "dep") is False
    assert teardown_owns_cluster(None, "dep") is False


import pytest


@pytest.mark.asyncio
async def test_the_wrapper_refuses_a_pointed_at_cluster(monkeypatch):
    """The guard must sit in the I/O path, not just exist as a helper: a
    deployment configured with someone else's cluster name must skip before
    any cloud call is made."""
    from worker import gke_cluster_reaper as mod

    monkeypatch.setenv("MODAL_APP_NAME", "dep")
    monkeypatch.setattr(mod.settings, "gke_cluster_name", "shared-cluster")
    monkeypatch.setattr(mod.settings, "gke_project_id", "p")

    def _boom(*a, **k):
        raise AssertionError("cloud APIs were touched for a non-owned cluster")

    monkeypatch.setattr(
        "worker.runtime._materialize_gcp_adc_credentials", _boom, raising=False
    )
    outcome = await mod.teardown_deployment_cluster()
    assert "derived name" in outcome


@pytest.mark.asyncio
async def test_the_reaper_refuses_a_pointed_at_cluster(monkeypatch):
    """The hourly reaper judges idleness from THIS deployment's database,
    which knows nothing about a shared cluster's other users. It must obey
    the same ownership rule as close-time teardown, before any cloud call."""
    from worker import gke_cluster_reaper as mod

    monkeypatch.setenv("MODAL_APP_NAME", "dep")
    monkeypatch.setattr(mod.settings, "gke_cluster_name", "shared-cluster")
    monkeypatch.setattr(mod.settings, "gke_region", "r")
    monkeypatch.setattr(mod.settings, "gke_project_id", "p")
    monkeypatch.setattr(mod.settings, "gke_idle_cluster_ttl_hours", 1.0)

    def _boom(*a, **k):
        raise AssertionError("cloud APIs were touched for a non-owned cluster")

    monkeypatch.setattr(
        "worker.runtime._materialize_gcp_adc_credentials", _boom, raising=False
    )
    outcome = await mod.reap_idle_cluster()
    assert "derived name" in outcome


@pytest.mark.asyncio
async def test_deploy_time_identity_beats_the_container_environment(monkeypatch):
    """A runtime secret can overwrite the app name in the container env. The
    identity a deletion authorizes against is the one bound at deploy time,
    passed in explicitly, so the env value must be ignored when it is given."""
    from worker import gke_cluster_reaper as mod

    monkeypatch.setenv("MODAL_APP_NAME", "impostor")
    monkeypatch.setattr(mod.settings, "gke_cluster_name", "real-trials")
    monkeypatch.setattr(mod.settings, "gke_project_id", "p")
    monkeypatch.setattr(mod.settings, "gke_region", "r")
    monkeypatch.setattr(mod.settings, "gke_idle_cluster_ttl_hours", 1.0)

    # With the deploy-bound name the cluster IS owned; the env impostor would
    # have refused it. Stop at the next step (credentials) to prove we got
    # past the guard.
    sentinel = {"reached": False}

    def _mark(*a, **k):
        sentinel["reached"] = True
        raise RuntimeError("stop here")

    monkeypatch.setattr(
        "worker.runtime._materialize_gcp_adc_credentials", _mark, raising=False
    )
    with pytest.raises(RuntimeError, match="stop here"):
        await mod.reap_idle_cluster(deploy_app_name="real")
    assert sentinel["reached"], "the deploy-bound identity did not authorize"


@pytest.mark.asyncio
async def test_teardown_waits_until_the_cluster_is_gone(monkeypatch):
    """The pinned harbor raises on a cluster it finds in STOPPING, so the
    delete must be awaited: a redeploy that returns while the cluster is
    half-deleted hands the race straight to the next trial."""
    from google.api_core import exceptions as gcp_exceptions
    from worker import gke_cluster_reaper as mod

    monkeypatch.setenv("MODAL_APP_NAME", "dep")
    monkeypatch.setattr(mod.settings, "gke_cluster_name", "dep-trials")
    monkeypatch.setattr(mod.settings, "gke_project_id", "p")
    monkeypatch.setattr(
        "worker.runtime._materialize_gcp_adc_credentials", lambda: None, raising=False
    )

    class _Cluster:
        name = "dep-trials"
        resource_labels = {"harbor-managed": "true"}
        location = "loc"

    polls = {"n": 0}

    class _Manager:
        def list_clusters(self, parent):
            class _L:
                clusters = [_Cluster()]

            return _L()

        def delete_cluster(self, name):
            return None

        def get_cluster(self, name):
            polls["n"] += 1
            if polls["n"] >= 3:
                raise gcp_exceptions.NotFound("gone")
            return _Cluster()

    class _CV1:
        @staticmethod
        def ClusterManagerClient(credentials=None):
            return _Manager()

    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (object(), "p"))
    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(
        sys.modules, "google.cloud.container_v1", SimpleNamespace(**vars(_CV1))
    )
    monkeypatch.setitem(
        sys.modules,
        "google.cloud",
        SimpleNamespace(container_v1=SimpleNamespace(**vars(_CV1))),
    )

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    outcome = await mod.teardown_deployment_cluster()
    assert outcome == "deleted 1 cluster(s)"
    assert polls["n"] >= 3, "the delete was not awaited to absence"
